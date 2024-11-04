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

	"github.com/PingCAP-QE/ee-apps/publisher/pkg/impl"
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

	config, err := loadConfig(*configFile)
	if err != nil {
		log.Fatal().Err(err).Msg("load config failed")
	}

	tiupPublishRequestKafkaReader, tiupWorker := initTiupWorkerFromConfig(config.Tiup)
	fsPublishRequestKafkaReader, fsWorker := initFsWorkerFromConfig(config.FileServer)

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

	// Start workers.
	var wg sync.WaitGroup
	ctx, cancel := context.WithCancel(context.Background())
	startWorker(ctx, &wg, tiupPublishRequestKafkaReader, tiupWorker)
	startWorker(ctx, &wg, fsPublishRequestKafkaReader, fsWorker)

	// Wait for signal.
	log.Warn().Msgf("exiting (%v)", <-errc)
	// Send cancellation signal to the goroutines.
	cancel()
	wg.Wait()
	log.Warn().Msg("exited")
}

func startWorker(ctx context.Context, wg *sync.WaitGroup, reader *kafka.Reader, worker impl.Worker) {
	if reader == nil {
		log.Warn().Msg("empty kafka reader, skip")
		return
	}
	if worker == nil {
		log.Warn().Msg("empty worker, skip")
		return
	}

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
				worker.Handle(cloudEvent)
			}
		}
	}()

	log.Info().Msg("Kafka consumer started")
}
