package main

import (
	"context"
	"encoding/json"
	"flag"
	"os"
	"strconv"

	"github.com/cloudevents/sdk-go/v2/event"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"github.com/segmentio/kafka-go"
	"gopkg.in/yaml.v3"
)

func main() {
	logLevel, err := strconv.Atoi(os.Getenv("LOG_LEVEL"))
	if err != nil {
		logLevel = int(zerolog.InfoLevel) // default to INFO
	}
	log.Logger = log.Level(zerolog.Level(logLevel))

	if os.Getenv("APP_ENV") == "development" {
		zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
		log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr})
	}

	configFile := flag.String("config", "./config.yaml", "Path to config file")
	flag.Parse()

	configData, err := os.ReadFile(*configFile)
	if err != nil {
		log.Fatal().Err(err).Msg("Error reading config file")
	}

	var config Config
	if err := yaml.Unmarshal(configData, &config); err != nil {
		log.Fatal().Err(err).Msg("Error parsing config file")
	}
	handler, err := NewHandler(config.MirrorUrl)
	if err != nil {
		log.Fatal().Err(err).Msg("Error creating handler")
	}

	reader := kafka.NewReader(kafka.ReaderConfig{
		Brokers:        config.Brokers,
		Topic:          config.Topic,
		GroupID:        config.ConsumerGroup,
		MinBytes:       10e3,
		MaxBytes:       10e6,
		CommitInterval: 5000,
		Logger:         kafka.LoggerFunc(log.Printf),
	})
	defer reader.Close()

	for {
		msg, err := reader.ReadMessage(context.Background())
		if err != nil {
			log.Err(err).Msg("Error reading message")
			continue
		}

		var cloudEvent event.Event
		if err := json.Unmarshal(msg.Value, &cloudEvent); err != nil {
			log.Err(err).Msg("Error unmarshaling CloudEvent")
			continue
		}

		log.Debug().Str("ce-id", cloudEvent.ID()).Str("ce-type", cloudEvent.Type()).Msg("received cloud event")
		handler.Handle(cloudEvent)
	}
}
