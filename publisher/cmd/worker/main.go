package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/cloudevents/sdk-go/v2/event"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"github.com/segmentio/kafka-go"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/tiup"
)

func main() {
	// Parse command-line flags
	var (
		configFile = flag.String("config", "config.yaml", "Path to config file")
		dbgF       = flag.Bool("debug", false, "Enable debug mode")
	)
	flag.Parse()

	if *dbgF {
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
		log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr}).With().Timestamp().Logger()
		log.Debug().Msg("debug logs enabled")
	} else {
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}

	// Load and parse configuration
	var config config.Worker
	{
		configData, err := os.ReadFile(*configFile)
		if err != nil {
			log.Fatal().Err(err).Msg("Error reading config file")
		}
		if err := yaml.Unmarshal(configData, &config); err != nil {
			log.Fatal().Err(err).Msg("Error parsing config file")
		}
	}

	ctx := log.Logger.WithContext(context.Background())
	// Create TiUP publisher
	var handler *tiup.Publisher
	{
		var err error
		handler, err = tiup.NewPublisher(config.MirrorUrl, config.LarkWebhookURL, &log.Logger)
		if err != nil {
			log.Fatal().Err(err).Msg("Error creating handler")
		}
	}

	// Configure Kafka reader
	var reader *kafka.Reader
	{
		reader = kafka.NewReader(kafka.ReaderConfig{
			Brokers:        config.Kafka.Brokers,
			Topic:          config.Kafka.Topic,
			GroupID:        config.Kafka.ConsumerGroup,
			MinBytes:       10e3,
			MaxBytes:       10e6,
			CommitInterval: 5000,
			Logger:         kafka.LoggerFunc(log.Printf),
		})
	}

	// Create channel used by both the signal handler and server goroutines
	// to notify the main goroutine when to stop the server.
	errc := make(chan error)

	// Setup interrupt handler. This optional step configures the process so
	// that SIGINT and SIGTERM signals cause the services to stop gracefully.
	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGINT, syscall.SIGTERM)
		errc <- fmt.Errorf("%s", <-c)
	}()

	var wg sync.WaitGroup
	ctx, cancel := context.WithCancel(ctx)

	Start(ctx, reader, handler, &wg, errc)

	// Wait for signal.
	log.Warn().Msgf("exiting (%v)", <-errc)

	// Send cancellation signal to the goroutines.
	cancel()

	wg.Wait()
	log.Warn().Msg("exited")
}

func Start(ctx context.Context, reader *kafka.Reader, handler *tiup.Publisher, wg *sync.WaitGroup, errc chan error) {
	(*wg).Add(1)
	go func() {
		defer (*wg).Done()
		defer reader.Close()

		for {
			select {
			case <-ctx.Done():
				return
			default:
				msg, err := reader.ReadMessage(ctx)
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
	}()

	log.Info().Msg("Kafka consumer started")
}
