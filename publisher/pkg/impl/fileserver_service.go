package impl

import (
	"context"
	"fmt"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	fileserver "github.com/PingCAP-QE/ee-apps/publisher/gen/fileserver"
)

// fileserver service example implementation.
// The example methods log the requests and return zero values.
type fileserversrvc struct {
	logger      *zerolog.Logger
	kafkaWriter *kafka.Writer
	redisClient redis.Cmdable
	eventSource string
	stateTTL    time.Duration
}

// NewFileserver returns the fileserver service implementation.
func NewFileserver(logger *zerolog.Logger, kafkaWriter *kafka.Writer, redisClient redis.Cmdable, eventSrc string) fileserver.Service {
	return &fileserversrvc{
		logger:      logger,
		kafkaWriter: kafkaWriter,
		redisClient: redisClient,
		eventSource: eventSrc,
		stateTTL:    DefaultStateTTL,
	}
}

// RequestToPublish implements request-to-publish.
func (s *fileserversrvc) RequestToPublish(ctx context.Context, p *fileserver.RequestToPublishPayload) (res []string, err error) {
	s.logger.Info().Msgf("fileserver.request-to-publish")
	// 1. Analyze the artifact_url to get the repo and tag and the tiup package information.
	publishRequests, err := analyzeFsFromOciArtifactUrl(p.ArtifactURL)
	if err != nil {
		return nil, err
	}

	// 2. Compose cloud events with the analyzed results.
	events := s.composeEvents(publishRequests)

	// 3. Send it to kafka topic with the request id as key and the event as value.
	var messages []kafka.Message
	for _, event := range events {
		bs, _ := event.MarshalJSON()
		messages = append(messages, kafka.Message{
			Key:   []byte(event.ID()),
			Value: bs,
		})
	}
	err = s.kafkaWriter.WriteMessages(ctx, messages...)
	if err != nil {
		return nil, fmt.Errorf("failed to send message to Kafka: %v", err)
	}

	var requestIDs []string
	for _, event := range events {
		requestIDs = append(requestIDs, event.ID())
	}

	// 4. Init the request dealing status in redis with the request id.
	for _, requestID := range requestIDs {
		if err := s.redisClient.SetNX(ctx, requestID, PublishStateQueued, s.stateTTL).Err(); err != nil {
			return nil, fmt.Errorf("failed to set initial status in Redis: %v", err)
		}
	}

	// 5. Return the request id.
	return requestIDs, nil
}

// QueryPublishingStatus implements query-publishing-status.
func (s *fileserversrvc) QueryPublishingStatus(ctx context.Context, p *fileserver.QueryPublishingStatusPayload) (res string, err error) {
	s.logger.Info().Msgf("fileserver.query-publishing-status")
	// 1. Get the request dealing status from redis with the request id.
	status, err := s.redisClient.Get(ctx, p.RequestID).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("request ID not found")
		}
		return "", fmt.Errorf("failed to get status from Redis: %v", err)
	}

	// 2. Return the request dealing status.
	return status, nil
}

func (s *fileserversrvc) composeEvents(requests []PublishRequest) []cloudevents.Event {
	var ret []cloudevents.Event
	for _, request := range requests {
		event := cloudevents.NewEvent()
		event.SetID(uuid.New().String())
		event.SetType(EventTypeFsPublishRequest)
		event.SetSource(s.eventSource)
		event.SetSubject(request.Publish.Name)
		event.SetData(cloudevents.ApplicationJSON, request)
		ret = append(ret, event)
	}

	return ret
}
