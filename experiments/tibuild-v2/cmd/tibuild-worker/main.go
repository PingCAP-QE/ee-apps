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
	"slices"
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

	cfg, err := config.NewWatcher(*configFile, config.WatcherOptions{
		Logger: &logger,
		// Defaults and the worker identity are process-level overrides: they are
		// applied to every revision before it is validated and published.
		Normalize: func(s *config.Service) {
			s.Worker.ApplyDefaults()
			if *workerID != "" {
				s.Worker.ID = *workerID
			}
		},
		Validate: validateWorkerConfig,
	})
	if err != nil {
		logger.Fatal().Err(err).Msg("failed to load configuration")
	}

	workerCfg := cfg.Get().Worker

	// A publish target typo would leave those steps pending forever, so it is
	// fatal. The rules file is mounted by Flux and may not exist yet on the
	// very first start, which is only a warning.
	if err := impl.ValidatePublishTargets(cfg.Get()); err != nil {
		if errors.Is(err, impl.ErrPublishRulesUnavailable) {
			logger.Warn().Err(err).Msg("skipping the publish target validation: delivery rules are not mounted yet")
		} else {
			logger.Fatal().Err(err).Msg("invalid publish target configuration")
		}
	}

	worker, err := impl.NewWorker(&logger, cfg.Get())
	if err != nil {
		logger.Fatal().Err(err).Msg("failed to initialize worker")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *healthAddr != "" {
		server := &http.Server{
			Addr:              *healthAddr,
			Handler:           newHealthMux(worker.MetricsHandler()),
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

func newHealthMux(metrics http.Handler) http.Handler {
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	}
	mux.HandleFunc("/livez", ok)
	mux.HandleFunc("/healthz", ok)
	if metrics != nil {
		mux.Handle("/metrics", metrics)
	}
	return mux
}

// validateWorkerConfig applies the worker-specific rules to a revision: the
// nightly schedule NewPublish needs, and worker.targets against the catalogue of
// the declared kinds. A TiUP worker declares its mirror shard in
// publish.targets, so that deployment-owned list is the catalogue; image workers
// have none here because their registry hosts are checked against delivery.yaml
// by ValidatePublishTargets.
func validateWorkerConfig(s *config.Service) error {
	if err := s.Publish.Nightly.Validate(); err != nil {
		return err
	}
	var knownTargets []string
	if slices.Contains(s.Worker.Kinds, impl.PublishKindTiUP) {
		knownTargets = s.Publish.Targets
	}
	return s.Worker.Validate(knownTargets)
}
