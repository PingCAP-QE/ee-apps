package fileserver

import (
	"context"
	"fmt"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/fileserver"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// artifact service example implementation.
type fileserversrvc struct {
	*share.BaseService
}

// NewService returns the tiup service implementation.
func NewService(logger *zerolog.Logger, cfg config.Service) fileserver.Service {
	return &fileserversrvc{
		BaseService: share.NewBaseServiceService(logger, cfg),
	}
}

// RequestToPublish implements request-to-publish.
func (s *fileserversrvc) RequestToPublish(ctx context.Context, p *fileserver.RequestToPublishPayload) (res []string, err error) {
	s.Logger.Info().Msgf("fileserver.request-to-publish")
	// 1. Analyze the artifact_url to get the repo and tag and the tiup package information.
	publishRequest, err := analyzeFsFromOciArtifactUrl(p.ArtifactURL)
	if err != nil {
		return nil, err
	}

	// 2. Compose cloud events with the analyzed results.
	events := s.composeEvents(publishRequest)

	// 3. Send it to kafka topic with the request id as key and the event as value.
	var messages []kafka.Message
	for _, event := range events {
		bs, _ := event.MarshalJSON()
		messages = append(messages, kafka.Message{
			Key:   []byte(event.ID()),
			Value: bs,
		})
	}
	err = s.KafkaWriter.WriteMessages(ctx, messages...)
	if err != nil {
		return nil, fmt.Errorf("failed to send message to Kafka: %v", err)
	}

	var requestIDs []string
	for _, event := range events {
		requestIDs = append(requestIDs, event.ID())
	}

	// 4. Init the request dealing status in redis with the request id.
	for _, requestID := range requestIDs {
		if err := s.RedisClient.SetNX(ctx, requestID, share.PublishStateQueued, s.StateTTL).Err(); err != nil {
			return nil, fmt.Errorf("failed to set initial status in Redis: %v", err)
		}
	}

	// 5. Return the request id.
	return requestIDs, nil
}

// QueryPublishingStatus implements query-publishing-status.
func (s *fileserversrvc) QueryPublishingStatus(ctx context.Context, p *fileserver.QueryPublishingStatusPayload) (res string, err error) {
	s.Logger.Info().Msgf("fileserver.query-publishing-status")
	return share.QueryStatusFromRedis(ctx, s.RedisClient, p.RequestID)
}

func (s *fileserversrvc) composeEvents(request *PublishRequestFS) []cloudevents.Event {
	var ret []cloudevents.Event
	event := cloudevents.NewEvent()
	event.SetID(uuid.New().String())
	event.SetType(EventTypeFsPublishRequest)
	event.SetSource(s.EventSource)
	event.SetSubject(request.Publish.Repo)
	event.SetData(cloudevents.ApplicationJSON, request)
	ret = append(ret, event)

	return ret
}
