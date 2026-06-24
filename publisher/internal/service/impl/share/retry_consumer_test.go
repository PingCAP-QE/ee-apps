package share

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
)

// MockWorker is a mock implementation of impl.Worker
type MockWorker struct {
	mock.Mock
}

func (m *MockWorker) Handle(event cloudevents.Event) cloudevents.Result {
	args := m.Called(event)
	return args.Get(0).(cloudevents.Result)
}

func setupMiniredis(t *testing.T) (*miniredis.Miniredis, redis.Cmdable) {
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatal(err)
	}
	redisClient := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	return mr, redisClient
}

func TestRetryableConsumer_Handle_Success(t *testing.T) {
	// Setup
	worker := new(MockWorker)
	mr, redisClient := setupMiniredis(t)
	defer mr.Close()
	dlqWriter := &kafka.Writer{}
	logger := zerolog.Nop()

	rc := NewRetryableConsumer(
		worker,
		redisClient,
		dlqWriter,
		logger,
		3,
		10*time.Second,
		5*time.Minute,
		"dlq-topic",
		"source-topic",
	)

	event := cloudevents.NewEvent()
	event.SetID("test-event-1")
	event.SetType("test.type")
	event.SetSource("test/source")

	// Mock worker to return ACK
	worker.On("Handle", event).Return(cloudevents.ResultACK)

	// Execute
	result := rc.Handle(event)

	// Assert
	assert.True(t, cloudevents.IsACK(result))
	worker.AssertExpectations(t)
}

func TestRetryableConsumer_Handle_NACK_IncrementsRetry(t *testing.T) {
	// Setup
	worker := new(MockWorker)
	mr, redisClient := setupMiniredis(t)
	defer mr.Close()
	dlqWriter := &kafka.Writer{}
	logger := zerolog.Nop()

	rc := NewRetryableConsumer(
		worker,
		redisClient,
		dlqWriter,
		logger,
		3,
		10*time.Second,
		5*time.Minute,
		"dlq-topic",
		"source-topic",
	)

	event := cloudevents.NewEvent()
	event.SetID("test-event-2")
	event.SetType("test.type")
	event.SetSource("test/source")

	// Mock worker to return NACK
	worker.On("Handle", event).Return(cloudevents.NewReceipt(false, "test error"))

	// Execute
	result := rc.Handle(event)

	// Assert
	assert.True(t, cloudevents.IsNACK(result))
	worker.AssertExpectations(t)

	// Verify retry count was incremented
	key := DLQRetryKeyPrefix + "test-event-2"
	val, err := redisClient.Get(context.Background(), key).Result()
	assert.NoError(t, err)
	assert.Equal(t, "1", val)
}

func TestRetryableConsumer_Handle_ExceedsMaxRetries_SendsToDLQ(t *testing.T) {
	// This test would require mocking the Kafka writer
	// For now, we'll test the logic without actual Kafka
	worker := new(MockWorker)
	mr, redisClient := setupMiniredis(t)
	defer mr.Close()
	dlqWriter := &kafka.Writer{}
	logger := zerolog.Nop()

	rc := NewRetryableConsumer(
		worker,
		redisClient,
		dlqWriter,
		logger,
		2, // max retries = 2
		10*time.Second,
		5*time.Minute,
		"dlq-topic",
		"source-topic",
	)

	event := cloudevents.NewEvent()
	event.SetID("test-event-3")
	event.SetType("test.type")
	event.SetSource("test/source")

	// Set retry count to 2 (exceeds max)
	key := DLQRetryKeyPrefix + "test-event-3"
	redisClient.Set(context.Background(), key, "2", 0)

	// Execute
	result := rc.Handle(event)

	// Assert - should be ACK (routed to DLQ)
	assert.True(t, cloudevents.IsACK(result))
	worker.AssertNotCalled(t, "Handle", event)
}

func TestRetryableConsumer_calculateBackoff(t *testing.T) {
	rc := &RetryableConsumer{
		backoffBase: 10 * time.Second,
		maxBackoff:  5 * time.Minute,
	}

	// Test backoff calculation
	backoff1 := rc.calculateBackoff(1)
	backoff2 := rc.calculateBackoff(2)
	backoff3 := rc.calculateBackoff(3)

	// Backoff should increase exponentially
	assert.True(t, backoff1 < backoff2)
	assert.True(t, backoff2 < backoff3)

	// Should not exceed max backoff
	assert.True(t, backoff1 <= rc.maxBackoff)
	assert.True(t, backoff2 <= rc.maxBackoff)
	assert.True(t, backoff3 <= rc.maxBackoff)
}
