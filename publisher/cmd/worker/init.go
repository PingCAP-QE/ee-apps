package main

import (
	"context"
	"encoding/json"

	"github.com/cloudevents/sdk-go/v2/event"
	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

type workerFactory func(*zerolog.Logger, redis.UniversalClient, map[string]string) (impl.Worker, error)

func newWorkerFunc(ctx context.Context, workerName string, wf workerFactory, workerCfg *config.Worker) func() {
	wl := log.With().Ctx(ctx).Str("worker", workerName).Logger()

	reader, worker, err := initWorkerFromConfig(workerCfg, wf, &wl)
	if err != nil {
		wl.Err(err).Msg("Error initializing worker")
		return nil
	}
	if reader == nil {
		wl.Warn().Msg("empty kafka reader, skip")
		return nil
	}
	if worker == nil {
		wl.Warn().Msg("empty worker, skip")
		return nil
	}

	return func() {
		defer reader.Close()
		wl.Info().Msg("Kafka consumer started")

		for {
			select {
			case <-ctx.Done():
				return
			default:
				msg, err := reader.ReadMessage(ctx)
				if err != nil {
					wl.Err(err).Msg("Error reading message")
					continue
				}

				var cloudEvent event.Event
				if err := json.Unmarshal(msg.Value, &cloudEvent); err != nil {
					wl.Err(err).Msg("Error unmarshaling CloudEvent")
					continue
				}

				wl.Debug().
					Str("ce-id", cloudEvent.ID()).
					Str("ce-type", cloudEvent.Type()).
					Str("ce-subject", cloudEvent.Subject()).
					Msg("received cloud event")
				worker.Handle(cloudEvent)
			}
		}
	}
}

func initWorkerFromConfig(cfg *config.Worker, wf workerFactory, wl *zerolog.Logger) (*kafka.Reader, impl.Worker, error) {
	if cfg == nil {
		return nil, nil, nil
	}

	// Configure Redis client.
	redisClient := redis.NewClient(&redis.Options{
		Addr:     cfg.Redis.Addr,
		Password: cfg.Redis.Password,
		Username: cfg.Redis.Username,
		DB:       cfg.Redis.DB,
	})
	_, err := redisClient.Ping(context.Background()).Result()
	if err != nil {
		wl.Err(err).Msg("Failed to connect to Redis")
		return nil, nil, err
	}

	worker, err := wf(wl, redisClient, cfg.Options)
	if err != nil {
		return nil, nil, err
	}

	kafkaReader := kafka.NewReader(kafka.ReaderConfig{
		Brokers:        cfg.Kafka.Brokers,
		Topic:          cfg.Kafka.Topic,
		GroupID:        cfg.Kafka.ConsumerGroup,
		MinBytes:       10e3,
		MaxBytes:       10e6,
		CommitInterval: 5000,
		Logger:         kafka.LoggerFunc(log.Printf),
	})

	return kafkaReader, worker, nil
}
