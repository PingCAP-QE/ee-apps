package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"

	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog/log"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/impl"
)

// Load and parse configuration.
func loadConfig(configFile string) (config.Worker, error) {
	var config config.Worker
	{
		configData, err := os.ReadFile(configFile)
		if err != nil {
			return config, fmt.Errorf("error reading config file: %v", err)
		}
		if err := yaml.Unmarshal(configData, &config); err != nil {
			return config, fmt.Errorf("error parsing config file: %v", err)
		}
	}
	return config, nil
}

func initTiupWorkerFromConfig(configFile string) (*kafka.Reader, impl.Worker, error) {
	config, err := loadConfig(configFile)
	if err != nil {
		return nil, nil, err
	}

	// Configure Redis client.
	redisClient := redis.NewClient(&redis.Options{
		Addr:     config.Redis.Addr,
		Password: config.Redis.Password,
		Username: config.Redis.Username,
		DB:       config.Redis.DB,
	})

	worker, err := impl.NewTiupWorker(&log.Logger, redisClient, config.Options)
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

	return kafkaReader, worker, nil
}

func initFsWorkerFromConfig(configFile string) (*kafka.Reader, impl.Worker) {
	config, err := loadConfig(configFile)
	if err != nil {
		log.Fatal().Err(err).Msg("load config failed")
	}

	// Configure Redis client.
	redisClient := redis.NewClient(&redis.Options{
		Addr:     config.Redis.Addr,
		Password: config.Redis.Password,
		Username: config.Redis.Username,
		DB:       config.Redis.DB,
	})

	var worker impl.Worker
	{
		worker, err = impl.NewFsWorker(&log.Logger, redisClient, config.Options)
		if err != nil {
			log.Fatal().Err(err).Msg("Error creating tiup publishing worker")
		}
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
