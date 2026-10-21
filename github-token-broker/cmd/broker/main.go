package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authn"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/cache"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/githubapp"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/ratelimit"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/server"
	tokenserver "github.com/PingCAP-QE/ee-apps/github-token-broker/internal/service/gen/http/token/server"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/service/gen/token"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/service/impl"
)

func main() {
	configPath := flag.String("config", envOrDefault("BROKER_CONFIG", "/etc/github-token-broker/config.yaml"), "path to the configuration file")
	flag.Parse()

	logger := newLogger()
	store, err := config.NewStore(*configPath, logger)
	if err != nil {
		logger.Error("failed to load configuration", "error", err)
		os.Exit(1)
	}
	cfg := store.Current()

	minter, err := githubapp.New(cfg.GitHub.AppID, cfg.GitHub.PrivateKeyPath, cfg.GitHub.APIURL, cfg.GitHub.RequestTimeout)
	if err != nil {
		logger.Error("failed to initialise github app client", "error", err)
		os.Exit(1)
	}
	reviewer, err := authn.New(cfg.Auth.KubernetesAPIURL, cfg.Auth.TokenFile, cfg.Auth.CAFile, cfg.Auth.RequestTimeout)
	if err != nil {
		logger.Error("failed to initialise token reviewer", "error", err)
		os.Exit(1)
	}
	brokerMetrics, err := metrics.New()
	if err != nil {
		logger.Error("failed to initialise metrics", "error", err)
		os.Exit(1)
	}

	tokenService := impl.NewToken(impl.TokenOptions{
		Config:    store,
		Minter:    minter,
		Cache:     cache.New(cfg.Cache.TTLMargin, cfg.Cache.MaxEntries),
		Metrics:   brokerMetrics,
		Logger:    logger,
		Readiness: reviewer.Ready,
	})
	endpoints := token.NewEndpoints(tokenService)

	mux := goahttp.NewMuxer()
	tokenServer := tokenserver.New(endpoints, mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, nil, nil)
	tokenserver.Mount(mux, tokenServer)

	limiter := ratelimit.New(cfg.RateLimit.PerMinute)
	middleware := server.NewMiddleware(reviewer, limiter, store, brokerMetrics, logger)
	handler := middleware.Wrap(mux)
	root := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			brokerMetrics.Handler().ServeHTTP(w, r)
			return
		}
		handler.ServeHTTP(w, r)
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if !cfg.Reload.Disabled {
		go store.Watch(ctx, cfg.Reload.Interval)
	}

	httpServer := &http.Server{
		Addr:         cfg.Server.Address,
		Handler:      root,
		ReadTimeout:  cfg.Server.ReadTimeout,
		WriteTimeout: cfg.Server.WriteTimeout,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		logger.Info("starting github-token-broker", "address", cfg.Server.Address, "app_id", cfg.GitHub.AppID)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server failed", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.Server.ShutdownTimeout)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		logger.Error("graceful shutdown failed", "error", err)
		os.Exit(1)
	}
	logger.Info("github-token-broker stopped")
}

func newLogger() *slog.Logger {
	level := slog.LevelInfo
	switch strings.ToLower(os.Getenv("LOG_LEVEL")) {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
