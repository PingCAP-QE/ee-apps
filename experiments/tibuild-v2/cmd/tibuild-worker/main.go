// Command tibuild-worker runs the tibuild background task executor as a
// standalone process. It shares the database and config schema with cmd/tibuild
// but does not serve the HTTP API.
package main

import (
	"context"
	"errors"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rs/zerolog"

	_ "github.com/go-sql-driver/mysql"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func main() {
	var (
		workerID   = flag.String("worker-id", "", "Unique worker identity written to claimed_by (defaults to the hostname)")
		configFile = flag.String("config", "config.yaml", "Path to config file")
		debug      = flag.Bool("debug", false, "Enable debug logging")
		healthAddr = flag.String("health-addr", ":8081", "Address for /livez and /healthz (empty disables the server)")
	)
	flag.Parse()

	level := zerolog.InfoLevel
	if *debug {
		level = zerolog.DebugLevel
	}
	zerolog.SetGlobalLevel(level)
	logger := zerolog.New(os.Stderr).With().Timestamp().Str("service", "tibuild-worker").Logger()

	cfg, err := config.Load(*configFile)
	if err != nil {
		logger.Fatal().Err(err).Msg("failed to load configuration")
	}

	workerCfg := cfg.Get().Worker
	workerCfg.ApplyDefaults()
	if *workerID != "" {
		workerCfg.ID = *workerID
	}
	if err := workerCfg.Validate(impl.DefaultWorkerTargets()); err != nil {
		logger.Fatal().Err(err).Msg("invalid worker configuration")
	}
	// NewWorker reads the same snapshot; publish the validated overrides
	// before any goroutine starts.
	cfg.Get().Worker = workerCfg

	worker, err := impl.NewWorker(&logger, cfg.Get())
	if err != nil {
		logger.Fatal().Err(err).Msg("failed to initialize worker")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *healthAddr != "" {
		server := &http.Server{
			Addr:              *healthAddr,
			Handler:           newHealthMux(),
			ReadHeaderTimeout: 5 * time.Second,
		}
		go func() {
			logger.Info().Str("addr", *healthAddr).Msg("starting worker health server")
			if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				logger.Err(err).Msg("worker health server failed")
				stop()
			}
		}()
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = server.Shutdown(shutdownCtx)
		}()
	}

	logger.Info().
		Str("worker_id", workerCfg.ID).
		Strs("targets", workerCfg.Targets).
		Int("concurrency", workerCfg.Concurrency).
		Msg("starting tibuild worker")

	worker.Start(ctx)
	worker.Stop()
	logger.Info().Msg("tibuild worker stopped")
}

func newHealthMux() http.Handler {
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	}
	mux.HandleFunc("/livez", ok)
	mux.HandleFunc("/healthz", ok)
	return mux
}
