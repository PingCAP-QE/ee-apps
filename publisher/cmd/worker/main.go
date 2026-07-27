package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/tiup"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
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

	cfgReloadable, err := config.NewReloadable[config.Workers](*configFile)
	if err != nil {
		log.Fatal().Err(err).Msg("load config failed")
	}
	cfg := cfgReloadable.Get()

	// Register reload handler for config changes.
	cfgReloadable.OnReload(func(newCfg *config.Workers) {
		log.Info().Msg("config reloaded - restart worker to apply Kafka/Redis changes")
	})

	// Create channel used by both the signal handler and server goroutines
	// to notify the main goroutine when to stop the server.
	errc := make(chan error)

	// Setup interrupt handler with SIGHUP for config reload.
	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
		for {
			sig := <-c
			if sig == syscall.SIGHUP {
				log.Info().Msg("received SIGHUP, reloading configuration")
				if err := cfgReloadable.Reload(); err != nil {
					log.Err(err).Msg("config reload error")
				}
			} else {
				errc <- fmt.Errorf("%s", sig)
				return
			}
		}
	}()

	// Start workers.
	var wg sync.WaitGroup
	ctx, cancel := context.WithCancel(context.Background())

	// tiup worker
	if workerFn := newWorkerFunc(ctx, "tiup", tiup.NewWorker, cfg.Tiup); workerFn != nil {
		wg.Go(workerFn)
	}

	// image worker
	if workerFn := newWorkerFunc(ctx, "image", image.NewWorker, cfg.Image); workerFn != nil {
		wg.Go(workerFn)
	}

	// Start auto-reload polling
	go cfgReloadable.AutoReload(ctx, 30*time.Second)

	// Wait for signal.
	log.Warn().Msgf("exiting (%v)", <-errc)
	// Send cancellation signal to the goroutines.
	cancel()
	wg.Wait()
	log.Warn().Msg("exited")
}
