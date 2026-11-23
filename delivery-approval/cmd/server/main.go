package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/approval"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/auth"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/server"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

func main() {
	configPath := flag.String("config", "config.yaml", "approval configuration file")
	flag.Parse()

	logger := log.New(os.Stderr, "[delivery-approval] ", log.Ltime)
	cfg, err := config.LoadConfig(*configPath)
	if err != nil {
		logger.Fatal(err)
	}
	manager := config.NewManager(cfg)

	dsn := os.Getenv("DL_APPROVAL_DATABASE_DSN")
	if dsn == "" {
		logger.Fatal("DL_APPROVAL_DATABASE_DSN is required")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	st, err := store.Open(startup, dsn)
	if err != nil {
		logger.Fatal(err)
	}
	defer st.Close()
	if err := st.Ready(startup); err != nil {
		logger.Fatal("approval schema unavailable; run the migration job first")
	}

	m := &metrics.Metrics{}
	authn := auth.NewGoogleValidator(manager, manager.Current().OIDCClientID)
	appServer := &http.Server{
		Addr:              manager.Current().Listen,
		Handler:           server.Application(server.Options{Store: st, Config: manager, Auth: authn, Metrics: m, Logger: logger}),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       time.Minute,
		MaxHeaderBytes:    16 * 1024,
	}
	probeServer := &http.Server{
		Addr:              manager.Current().HealthAddress,
		Handler:           server.Probes(server.Options{Store: st, Metrics: m, Logger: logger}),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       30 * time.Second,
	}

	go config.WatchFile(ctx, *configPath, logger, func() error {
		return manager.Reload(*configPath)
	})
	go approval.NewSweeper(st, nil, logger).Run(ctx)

	errc := make(chan error, 2)
	go func() { errc <- appServer.ListenAndServe() }()
	go func() { errc <- probeServer.ListenAndServe() }()
	logger.Printf("delivery-approval listening on %s (probes %s)", appServer.Addr, probeServer.Addr)

	select {
	case <-ctx.Done():
	case err = <-errc:
	}
	stop()

	shutdown, cancelShutdown := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancelShutdown()
	_ = probeServer.Shutdown(shutdown)
	if shutdownErr := appServer.Shutdown(shutdown); shutdownErr != nil {
		_ = appServer.Close()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatal("approval listener failed")
	}
}
