package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/PingCAP-QE/ee-apps/dl/internal/service/delivery"
	ocisvc "github.com/PingCAP-QE/ee-apps/dl/internal/service/oci"
	"github.com/PingCAP-QE/ee-apps/dl/internal/share"
	"github.com/PingCAP-QE/ee-apps/dl/pkg/reload"
)

func runDelivery(mode, address, healthAddress, configFile string, ociFile *string, allowed []string, limits delivery.Limits, logger *log.Logger) error {
	if mode != "public" && mode != "management" && mode != "delivery-migrate" {
		return errors.New("invalid server mode")
	}
	if limits.Concurrent < 1 || limits.RequestsPerSecond <= 0 || limits.Burst < 1 || limits.TransferTimeout <= 0 {
		return errors.New("invalid delivery limits")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	dsn := os.Getenv("DL_DELIVERY_DATABASE_URL")
	if dsn == "" {
		return errors.New("DL_DELIVERY_DATABASE_URL is required")
	}
	store, err := share.Open(startup, dsn)
	if err != nil {
		return err
	}
	defer store.DB.Close()
	if mode == "delivery-migrate" {
		if err := store.Migrate(startup); err != nil {
			return errors.New("delivery schema migration failed")
		}
		logger.Print("delivery schema ready")
		return nil
	}
	cfg, err := share.LoadConfig(configFile)
	if err != nil {
		return err
	}
	if err := store.DB.QueryRowContext(startup, `SELECT EXISTS(SELECT 1 FROM download_issuers)`).Scan(new(bool)); err != nil {
		return errors.New("delivery schema unavailable; apply migrations first")
	}
	oci := ocisvc.New(logger, ociFile, allowed)
	provider, ok := oci.(share.RepoProvider)
	if !ok {
		return errors.New("OCI delivery provider unavailable")
	}
	if reloader, ok := oci.(interface {
		Reload(context.Context, string) error
	}); ok {
		go reload.WatchFile(ctx, *ociFile, logger, func() error { return reloader.Reload(ctx, *ociFile) })
	}
	svc := &share.Service{Store: store, Backend: share.OCIBackend{Provider: provider}, Config: cfg, Logger: logger}
	var handler http.Handler
	if mode == "public" {
		handler = delivery.Public(svc, limits)
	} else {
		handler = delivery.Management(svc, limits)
	}
	srv := &http.Server{Addr: address, Handler: handler, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: time.Minute, MaxHeaderBytes: 16 * 1024}
	if mode == "management" {
		srv.ReadTimeout = 15 * time.Second
	}
	// Probes are deliberately on another port. The customer-facing handler has
	// no /healthz, /livez, fallback mux or request logging middleware.
	probes := http.NewServeMux()
	probes.Handle("GET /metrics", &svc.Metrics)
	probes.HandleFunc("GET /livez", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	probes.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		var ready bool
		if err := store.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM download_issuers)`).Scan(&ready); err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	health := &http.Server{Addr: healthAddress, Handler: probes, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second}
	errc := make(chan error, 2)
	go func() { errc <- srv.ListenAndServe() }()
	go func() { errc <- health.ListenAndServe() }()
	logger.Printf("delivery mode=%s listening", mode)
	select {
	case <-ctx.Done():
	case err = <-errc:
	}
	stop()
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancelShutdown()
	// Shutdown both listeners even when one failed to start.
	_ = health.Shutdown(shutdown)
	if shutdownErr := srv.Shutdown(shutdown); shutdownErr != nil {
		_ = srv.Close()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		return errors.New("delivery listener failed")
	}
	return nil
}
