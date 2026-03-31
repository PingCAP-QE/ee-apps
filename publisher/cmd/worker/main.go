package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/fileserver"
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

	cfg, err := config.Load[config.Workers](*configFile)
	if err != nil {
		log.Fatal().Err(err).Msg("load config failed")
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

	// Start workers.
	var wg sync.WaitGroup
	ctx, cancel := context.WithCancel(context.Background())

	// tiup worker
	if workerFn := newWorkerFunc(ctx, "tiup", tiup.NewWorker, cfg.Tiup); workerFn != nil {
		wg.Go(workerFn)
	}

	// fileserver worker
	if workerFn := newWorkerFunc(ctx, "fileserver", fileserver.NewWorker, cfg.FileServer); workerFn != nil {
		wg.Go(workerFn)
	}

	// Wait for signal.
	log.Warn().Msgf("exiting (%v)", <-errc)
	// Send cancellation signal to the goroutines.
	cancel()
	wg.Wait()
	log.Warn().Msg("exited")
}
