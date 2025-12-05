package tiup

import (
	"context"
	"fmt"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	gentiup "github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tiup"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// tiup service example implementation.
type tiupsrvc struct {
	*share.BaseService
	deliveryConfig *DeliveryConfig
}

// NewService returns the tiup service implementation.
func NewService(logger *zerolog.Logger, cfg config.Service) gentiup.Service {
	srvc := &tiupsrvc{BaseService: share.NewBaseServiceService(logger, cfg)}

	// load delivery config
	tiupCfg := cfg.Services["tiup"]
	switch v := tiupCfg.(type) {
	case map[string]any:
		deliveryConfigFile := v[tiupServiceDeliveryCfgKey]
		switch file := deliveryConfigFile.(type) {
		case string:
			// load the yaml from the file
			ret, err := config.Load[DeliveryConfig](file)
			if err != nil {
				srvc.Logger.Fatal().Err(err).Msgf("failed to load delivery config")
			}

			srvc.deliveryConfig = ret
		}
	}

	return srvc
}

// RequestToPublish implements delivery-by-rules).
func (s *tiupsrvc) DeliveryByRules(ctx context.Context, p *gentiup.DeliveryByRulesPayload) (res []string, err error) {
	s.Logger.Info().Msgf("tiup.delivery-by-rules")
	// skip when there is no delivery config
	if s.deliveryConfig == nil {
		return nil, nil
	}

	// 1. match for the rules
	RequestToPublishPayloads, err := analyzeTiupDeliveries(p.ArtifactURL, s.deliveryConfig.TiupPublishRules)
	if err != nil {
		return nil, err
	}

	// 2. Request to publish
	for _, payload := range RequestToPublishPayloads {
		ids, err := s.RequestToPublish(ctx, &payload)
		if err != nil {
			return nil, err
		}

		res = append(res, ids...)
	}

	return res, nil
}

// RequestToPublish implements request-to-publish.
func (s *tiupsrvc) RequestToPublish(ctx context.Context, p *gentiup.RequestToPublishPayload) (res []string, err error) {
	s.Logger.Info().Msgf("tiup.request-to-publish")
	// 1. Analyze the artifact_url to get the repo and tag and the tiup package information.
	publishRequests, err := analyzeTiupFromOciArtifactUrl(p.ArtifactURL, p.TiupMirror)
	if err != nil {
		return nil, err
	}
	if p.Version != nil && *p.Version != "" {
		for i := range publishRequests {
			publishRequests[i].Publish.Version = *p.Version
		}
	}

	return s.enqueueTiupPublishRequests(ctx, publishRequests)
}

// RequestToPublishSingle implements request-to-publish-single.
func (s *tiupsrvc) RequestToPublishSingle(ctx context.Context, p *gentiup.PublishRequestTiUP) (string, error) {
	s.Logger.Info().Msgf("tiup.request-to-publish-single")
	if p == nil {
		return "", fmt.Errorf("payload is nil")
	}
	requestIDs, err := s.enqueueTiupPublishRequests(ctx, []gentiup.PublishRequestTiUP{*p})
	if err != nil {
		return "", fmt.Errorf("failed to enqueue request: %v", err)
	}
	return requestIDs[0], nil
}

// QueryPublishingStatus implements query-publishing-status.
func (s *tiupsrvc) QueryPublishingStatus(ctx context.Context, p *gentiup.QueryPublishingStatusPayload) (res string, err error) {
	s.Logger.Info().Msgf("tiup.query-publishing-status")
	return share.QueryStatusFromRedis(ctx, s.RedisClient, p.RequestID)
}

// ResetRateLimit implements tiup.Service.
func (s *tiupsrvc) ResetRateLimit(ctx context.Context) error {
	// get the keys
	iter := s.RedisClient.Scan(ctx, 0, fmt.Sprintf("%s:*", redisKeyPrefixTiupRateLimit), 0).Iterator()
	var keys []string
	for iter.Next(ctx) {
		keys = append(keys, iter.Val())
	}
	if err := iter.Err(); err != nil {
		return fmt.Errorf("failed to scan keys: %v", err)
	}

	// delete the keys
	if len(keys) == 0 {
		return nil
	}
	if err := s.RedisClient.Del(ctx, keys...).Err(); err != nil {
		s.Logger.Err(err).Any("keys", keys).Msg("failed to delete keys")
		return fmt.Errorf("failed to delete keys: %v", err)
	}
	s.Logger.Debug().Any("keys", keys).Msg("deleted redis keys.")

	return nil
}

func (s *tiupsrvc) enqueueTiupPublishRequests(ctx context.Context, requests []gentiup.PublishRequestTiUP) ([]string, error) {
	// compose cloud events with the analyzed results.
	events := s.composeEvents(requests)

	// send it to kafka topic with the request id as key and the event as value.
	var messages []kafka.Message
	for _, event := range events {
		bs, _ := event.MarshalJSON()
		messages = append(messages, kafka.Message{
			Key:   []byte(event.ID()),
			Value: bs,
		})
	}
	err := s.KafkaWriter.WriteMessages(ctx, messages...)
	if err != nil {
		return nil, fmt.Errorf("failed to send message to Kafka: %v", err)
	}

	var requestIDs []string
	for _, event := range events {
		requestIDs = append(requestIDs, event.ID())
	}

	// init the request dealing status in redis with the request id.
	for _, requestID := range requestIDs {
		if err := s.RedisClient.SetNX(ctx, requestID, share.PublishStateQueued, s.StateTTL).Err(); err != nil {
			return nil, fmt.Errorf("failed to set initial status in Redis: %v", err)
		}
	}

	return requestIDs, nil
}

func (s *tiupsrvc) composeEvents(requests []gentiup.PublishRequestTiUP) []cloudevents.Event {
	var ret []cloudevents.Event
	for _, request := range requests {
		ret = append(ret, s.composeEvent(&request))
	}

	return ret
}

func (s *tiupsrvc) composeEvent(request *gentiup.PublishRequestTiUP) cloudevents.Event {
	event := s.BaseService.ComposeEvent(request)
	event.SetType(EventTypeTiupPublishRequest)
	event.SetSubject(request.TiupMirror)
	return event
}
