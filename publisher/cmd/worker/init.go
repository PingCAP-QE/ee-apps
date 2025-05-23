package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"

	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog/log"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/fileserver"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/tiup"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// Load and parse configuration.
func loadConfig(configFile string) (*config.Workers, error) {
	configData, err := os.ReadFile(configFile)
	if err != nil {
		return nil, fmt.Errorf("error reading config file: %v", err)
	}

	var config config.Workers
	if err := yaml.Unmarshal(configData, &config); err != nil {
		return nil, fmt.Errorf("error parsing config file: %v", err)
	}

	return &config, nil
}

func initTiupWorkerFromConfig(config *config.Worker) (*kafka.Reader, impl.Worker) {
	if config == nil {
		return nil, nil
	}

	// Configure Redis client.
	redisClient := redis.NewClient(&redis.Options{
		Addr:     config.Redis.Addr,
		Password: config.Redis.Password,
		Username: config.Redis.Username,
		DB:       config.Redis.DB,
	})

	worker, err := tiup.NewWorker(&log.Logger, redisClient, config.Options)
	if err != nil {
		log.Fatal().Err(err).Msg("Error creating tiup publishing worker")
	}

	kafkaReader := kafka.NewReader(kafka.ReaderConfig{
		Brokers:        config.Kafka.Brokers,
		Topic:          config.Kafka.Topic,
		GroupID:        config.Kafka.ConsumerGroup,
		MinBytes:       10e3,
		MaxBytes:       10e6,
		CommitInterval: 5000,
		Logger:         kafka.LoggerFunc(log.Printf),
	})

	return kafkaReader, worker
}

func initFsWorkerFromConfig(config *config.Worker) (*kafka.Reader, impl.Worker) {
	if config == nil {
		return nil, nil
	}

	// Configure Redis client.
	redisClient := redis.NewClient(&redis.Options{
		Addr:     config.Redis.Addr,
		Password: config.Redis.Password,
		Username: config.Redis.Username,
		DB:       config.Redis.DB,
	})

	worker, err := fileserver.NewWorker(&log.Logger, redisClient, config.Options)
	if err != nil {
		log.Fatal().Err(err).Msg("Error creating tiup publishing worker")
	}

	kafkaReader := kafka.NewReader(kafka.ReaderConfig{
		Brokers:        config.Kafka.Brokers,
		Topic:          config.Kafka.Topic,
		GroupID:        config.Kafka.ConsumerGroup,
		MinBytes:       10e3,
		MaxBytes:       10e6,
		CommitInterval: 5000,
		Logger:         kafka.LoggerFunc(log.Printf),
	})

	return kafkaReader, worker
}
