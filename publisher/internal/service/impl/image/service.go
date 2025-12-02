package image

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

// artifact service example implementation.
// The example methods log the requests and return zero values.
type imagesrvc struct {
	logger      *zerolog.Logger
	kafkaWriter *kafka.Writer
	redisClient redis.Cmdable
	eventSource string
	stateTTL    time.Duration
}

// NewService returns the image service implementation.
func NewService(logger *zerolog.Logger, kafkaWriter *kafka.Writer, redisClient redis.Cmdable, eventSrc string) image.Service {
	return &imagesrvc{
		logger:      logger,
		kafkaWriter: kafkaWriter,
		redisClient: redisClient,
		eventSource: eventSrc,
		stateTTL:    share.DefaultStateTTL,
	}
}

// RequestToCopy implements image.Service.
func (s *imagesrvc) RequestToCopy(ctx context.Context, p *image.RequestToCopyPayload) (res string, err error) {
	return s.enqueueRequest(ctx, share.EventTypeImagePublishRequest, p.Source, p)
}

// QueryCopyingStatus implements image.Service.
func (s *imagesrvc) QueryCopyingStatus(ctx context.Context, p *image.QueryCopyingStatusPayload) (string, error) {
	return share.QueryStatusFromRedis(ctx, s.redisClient, p.RequestID)
}

// RequestMultiarchCollect implements image.Service.
func (s *imagesrvc) RequestMultiarchCollect(ctx context.Context, p *image.RequestMultiarchCollectPayload) (res *image.RequestMultiarchCollectResult, err error) {
	requestID, err := s.enqueueRequest(ctx, share.EventTypeImageMultiArchCollectRequest, p.ImageURL, p)
	if err != nil {
		return nil, err
	}

	result := &image.RequestMultiarchCollectResult{
		Async: p.Async,
	}

	if p.Async {
		result.RequestID = &requestID
		return result, nil
	}

	// wait for the result.
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.C:
			state, err := s.QueryMultiarchCollectStatus(ctx, &image.QueryMultiarchCollectStatusPayload{
				RequestID: requestID,
			})
			if err != nil {
				return nil, err
			}

			if share.IsStateCompleted(state) {
				if state != share.PublishStateSuccess {
					return nil, fmt.Errorf("multiarch collect failed, task state: %s", state)
				}
				// Fetch the result from Redis
				resultJSON, err := s.redisClient.Get(ctx, fmt.Sprintf("%s-result", requestID)).Bytes()
				if err != nil {
					return nil, fmt.Errorf("failed to get result from redis: %w", err)
				}
				if err := json.Unmarshal(resultJSON, &result); err != nil {
					return nil, fmt.Errorf("failed to unmarshal result: %w", err)
				}
				return result, nil
			}
		}
	}
}

// QueryMultiarchCollectStatus implements image.Service.
func (s *imagesrvc) QueryMultiarchCollectStatus(ctx context.Context, p *image.QueryMultiarchCollectStatusPayload) (string, error) {
	return share.QueryStatusFromRedis(ctx, s.redisClient, p.RequestID)
}

// RequestToCopy implements image.Service.
func (s *imagesrvc) enqueueRequest(ctx context.Context, requestType, subject string, p any) (string, error) {
	// 1. generate a unique request ID
	requestID := uuid.New().String()

	// 2. Compose cloud events
	event := cloudevents.NewEvent()
	event.SetID(requestID)
	event.SetType(requestType)
	event.SetSource(s.eventSource)
	event.SetSubject(subject)
	event.SetData(cloudevents.ApplicationJSON, p)

	// 3. Send it to kafka topic with the request id as key and the event as value.
	bs, err := event.MarshalJSON()
	if err != nil {
		return "", fmt.Errorf("failed to marshal event: %v", err)
	}
	message := kafka.Message{
		Key:   []byte(event.ID()),
		Value: bs,
	}
	if err := s.kafkaWriter.WriteMessages(ctx, message); err != nil {
		return "", fmt.Errorf("failed to send message to Kafka: %v", err)
	}

	// 4. Init the request dealing status in redis with the request id.
	if err := s.redisClient.SetNX(ctx, requestID, share.PublishStateQueued, share.DefaultStateTTL).Err(); err != nil {
		return "", fmt.Errorf("failed to initialize request status: %v", err)
	}
	s.logger.Info().
		Str("request_type", share.EventTypeImagePublishRequest).
		Str("request_id", requestID).
		Any("request_payload", p).
		Msg("request queued")

	return requestID, nil
}
