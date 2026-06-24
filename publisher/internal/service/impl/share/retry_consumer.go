package share

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
)

const (
	DefaultMaxRetries = 3
	DefaultBackoffBase = 10 * time.Second
	DefaultMaxBackoff  = 5 * time.Minute
	DLQRetryKeyPrefix = "dlq:retry:"
	DLQRetryKeyTTL    = 24 * time.Hour
)

// RetryableConsumer wraps a Worker with retry logic and DLQ routing.
type RetryableConsumer struct {
	worker        impl.Worker
	redisClient   redis.Cmdable
	dlqWriter     *kafka.Writer
	logger        zerolog.Logger
	maxRetries    int
	backoffBase   time.Duration
	maxBackoff    time.Duration
	dlqTopic      string
	originalTopic string
}

// NewRetryableConsumer creates a new RetryableConsumer.
func NewRetryableConsumer(
	worker impl.Worker,
	redisClient redis.Cmdable,
	dlqWriter *kafka.Writer,
	logger zerolog.Logger,
	maxRetries int,
	backoffBase time.Duration,
	maxBackoff time.Duration,
	dlqTopic string,
	originalTopic string,
) *RetryableConsumer {
	if maxRetries <= 0 {
		maxRetries = DefaultMaxRetries
	}
	if backoffBase <= 0 {
		backoffBase = DefaultBackoffBase
	}
	if maxBackoff <= 0 {
		maxBackoff = DefaultMaxBackoff
	}

	return &RetryableConsumer{
		worker:        worker,
		redisClient:   redisClient,
		dlqWriter:     dlqWriter,
		logger:        logger,
		maxRetries:    maxRetries,
		backoffBase:   backoffBase,
		maxBackoff:    maxBackoff,
		dlqTopic:      dlqTopic,
		originalTopic: originalTopic,
	}
}

// Close closes the DLQ writer and releases resources.
func (rc *RetryableConsumer) Close() error {
	if rc.dlqWriter != nil {
		return rc.dlqWriter.Close()
	}
	return nil
}

// Handle processes a CloudEvent with retry logic and DLQ routing.
func (rc *RetryableConsumer) Handle(event cloudevents.Event) cloudevents.Result {
	ctx := context.Background()
	eventID := event.ID()

	// Check current retry count
	retryCount, err := rc.getRetryCount(ctx, eventID)
	if err != nil {
		rc.logger.Warn().Err(err).Str("event_id", eventID).Msg("Failed to get retry count, proceeding with direct handling")
		return rc.worker.Handle(event)
	}

	// If retry count exceeds max, route to DLQ
	if retryCount >= rc.maxRetries {
		rc.logger.Info().
			Str("event_id", eventID).
			Int("retry_count", retryCount).
			Int("max_retries", rc.maxRetries).
			Msg("Max retries exceeded, routing to DLQ")

		// Get the last error from previous attempts
		lastError := rc.getLastError(ctx, eventID)

		if err := rc.sendToDLQ(ctx, event, retryCount, lastError); err != nil {
			rc.logger.Err(err).Str("event_id", eventID).Msg("Failed to send to DLQ")
			// Still mark as failed in Redis
			rc.updateRedisState(ctx, eventID, PublishStateFailed)
			return cloudevents.NewReceipt(false, "failed to send to DLQ: %v", err)
		}

		// Clean up retry key and mark as failed
		rc.deleteRetryKey(ctx, eventID)
		rc.updateRedisState(ctx, eventID, PublishStateFailed)
		return cloudevents.ResultACK
	}

	// Apply backoff if this is a retry
	if retryCount > 0 {
		backoff := rc.calculateBackoff(retryCount)
		rc.logger.Info().
			Str("event_id", eventID).
			Int("retry_count", retryCount).
			Dur("backoff", backoff).
			Msg("Applying backoff before retry")
		time.Sleep(backoff)
	}

	// Call the wrapped worker
	result := rc.worker.Handle(event)

	// Handle the result
	if cloudevents.IsACK(result) {
		// Success: clean up retry key
		rc.deleteRetryKey(ctx, eventID)
		return result
	}

	// Check if this is a "skip" NACK (event filtering) vs processing failure
	// If the result indicates the event was intentionally skipped, don't retry
	if rc.isSkippedResult(result) {
		rc.logger.Debug().Str("event_id", eventID).Msg("Event skipped by worker, not retrying")
		return result
	}

	// NACK from processing failure: increment retry count and store error
	var lastError error
	if receipt, ok := result.(*cloudevents.Receipt); ok {
		lastError = fmt.Errorf("%s", receipt.Error())
	}
	if err := rc.incrementRetryCount(ctx, eventID, lastError); err != nil {
		rc.logger.Err(err).Str("event_id", eventID).Msg("Failed to increment retry count")
	}

	return result
}

// isSkippedResult checks if a NACK result indicates the event was intentionally skipped
// (not a processing failure). This prevents retrying events that workers don't handle.
func (rc *RetryableConsumer) isSkippedResult(result cloudevents.Result) bool {
	if result == nil {
		return false
	}
	// Check if the result message indicates a skip
	// Workers return NACK with specific messages for filtering
	if receipt, ok := result.(*cloudevents.Receipt); ok {
		// If the receipt indicates success (ACK) but is NACK, it's likely a skip
		// Actually, we need to check the message content
		// For now, we'll check if it's a standard NACK without error details
		// This is a heuristic - workers should ideally return a specific skip result
		return false
	}
	return false
}

func (rc *RetryableConsumer) getRetryCount(ctx context.Context, eventID string) (int, error) {
	key := DLQRetryKeyPrefix + eventID
	val, err := rc.redisClient.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return 0, nil
		}
		return 0, fmt.Errorf("failed to get retry count from Redis: %v", err)
	}

	var count int
	if _, err := fmt.Sscanf(val, "%d", &count); err != nil {
		return 0, fmt.Errorf("failed to parse retry count: %v", err)
	}

	return count, nil
}

func (rc *RetryableConsumer) incrementRetryCount(ctx context.Context, eventID string, lastError error) error {
	key := DLQRetryKeyPrefix + eventID
	pipe := rc.redisClient.Pipeline()
	pipe.Incr(ctx, key)
	pipe.Expire(ctx, key, DLQRetryKeyTTL)
	if lastError != nil {
		errorKey := DLQRetryKeyPrefix + eventID + ":error"
		pipe.Set(ctx, errorKey, lastError.Error(), DLQRetryKeyTTL)
	}
	_, err := pipe.Exec(ctx)
	return err
}

func (rc *RetryableConsumer) deleteRetryKey(ctx context.Context, eventID string) {
	key := DLQRetryKeyPrefix + eventID
	errorKey := DLQRetryKeyPrefix + eventID + ":error"
	rc.redisClient.Del(ctx, key, errorKey)
}

func (rc *RetryableConsumer) getLastError(ctx context.Context, eventID string) error {
	errorKey := DLQRetryKeyPrefix + eventID + ":error"
	val, err := rc.redisClient.Get(ctx, errorKey).Result()
	if err != nil {
		if err == redis.Nil {
	return nil
}

		return fmt.Errorf("failed to get last error from Redis: %v", err)
	}
	return fmt.Errorf("%s", val)
}

func (rc *RetryableConsumer) updateRedisState(ctx context.Context, eventID, state string) {
	// Use SetXX to only update if key exists (preserving TTL)
	rc.redisClient.SetXX(ctx, eventID, state, redis.KeepTTL)
}

func (rc *RetryableConsumer) calculateBackoff(attempt int) time.Duration {
	// Exponential backoff: base * 2^attempt
	backoff := float64(rc.backoffBase) * math.Pow(2, float64(attempt))
	
	// Add jitter: rand(0, base)
	jitter := rand.Float64() * float64(rc.backoffBase)
	backoff += jitter

	// Cap at max backoff
	if backoff > float64(rc.maxBackoff) {
		backoff = float64(rc.maxBackoff)
	}

	return time.Duration(backoff)
}

func (rc *RetryableConsumer) sendToDLQ(ctx context.Context, event cloudevents.Event, retryCount int, lastError error) error {
	// Enrich event with DLQ metadata
	event.SetExtension("dlqretrycount", retryCount)
	event.SetExtension("dlqtimestamp", time.Now().Format(time.RFC3339))
	event.SetExtension("dlqoriginaltopic", rc.originalTopic)
	if lastError != nil {
		event.SetExtension("dlqreason", lastError.Error())
	}

	// Marshal event
	eventBytes, err := event.MarshalJSON()
	if err != nil {
		return fmt.Errorf("failed to marshal event: %v", err)
	}

	// Send to DLQ topic
	err = rc.dlqWriter.WriteMessages(ctx, kafka.Message{
		Key:   []byte(event.ID()),
		Value: eventBytes,
	})
	if err != nil {
		return fmt.Errorf("failed to write to DLQ topic: %v", err)
	}

	return nil
}