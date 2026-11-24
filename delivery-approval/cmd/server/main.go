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
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/delivery"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/dl"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/lark"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/secure"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/server"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/web"
)

func main() {
	mode := flag.String("mode", "server", "run mode: server or migrate")
	configPath := flag.String("config", "config.yaml", "approval configuration file")
	flag.Parse()

	logger := log.New(os.Stderr, "[delivery-approval] ", log.Ltime)
	switch *mode {
	case "server":
	case "migrate":
		// The same image serves the one-shot migration Job; the runtime role
		// still owns no DDL, so the Job is the only writer of the schema.
		runMigrate(logger)
		return
	default:
		logger.Fatalf("unknown mode %q, want server or migrate", *mode)
	}

	cfg, err := config.LoadConfig(*configPath)
	if err != nil {
		logger.Fatal(err)
	}
	if v := os.Getenv("DL_APPROVAL_OIDC_CLIENT_ID"); v != "" {
		cfg.OIDCClientID = v
	}
	if cfg.OIDCClientID == "" {
		logger.Fatal("DL_APPROVAL_OIDC_CLIENT_ID or oidc_client_id is required")
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

	// Optional Lark notification + delivery wiring. A partially configured
	// provider is a startup error; a fully absent provider leaves delivery
	// disabled (the request/approval flow still works).
	var notifier *lark.Client
	appID, appSecret := os.Getenv("DL_APPROVAL_LARK_APP_ID"), os.Getenv("DL_APPROVAL_LARK_APP_SECRET")
	larkConfigured := appID != "" || appSecret != "" || cfg.LarkChatID != ""
	if larkConfigured {
		// A partially configured Lark provider is a startup error; a chat ID
		// alone is not enough to send anything.
		if appID == "" || appSecret == "" {
			logger.Fatal("Lark notifications require DL_APPROVAL_LARK_APP_ID and DL_APPROVAL_LARK_APP_SECRET")
		}
		notifier, err = lark.NewDefault(appID, appSecret, cfg.LarkChatID, manager, cfg.LarkBuzz, logger)
		if err != nil {
			logger.Fatal(err)
		}
	}

	var deliverySvc *delivery.Service
	var revoker approval.Revoker
	issuerSecret, keyEncoded := os.Getenv("DL_APPROVAL_DL_ISSUER_SECRET"), os.Getenv("DL_APPROVAL_URL_ENCRYPTION_KEY")
	if issuerSecret != "" || keyEncoded != "" {
		if issuerSecret == "" || keyEncoded == "" {
			logger.Fatal("delivery requires DL_APPROVAL_DL_ISSUER_SECRET and DL_APPROVAL_URL_ENCRYPTION_KEY")
		}
		key, err := secure.ParseKey(keyEncoded)
		if err != nil {
			logger.Fatal(err)
		}
		sealer, err := secure.New(key)
		if err != nil {
			logger.Fatal(err)
		}
		dlClient, err := dl.New(cfg.DLBaseURL, issuerSecret, nil)
		if err != nil {
			logger.Fatal(err)
		}
		revoker = dlClient
		if notifier != nil {
			deliverySvc = delivery.New(st, dlClient, sealer, notifier, logger)
		} else {
			logger.Print("delivery disabled: Lark notifier is not configured")
		}
	}

	var deliveryHandler web.Deliverer
	if deliverySvc != nil {
		deliveryHandler = deliverySvc
	}
	var approvalHandler web.ApprovalNotifier
	var decisionHandler web.DecisionNotifier
	if notifier != nil {
		approvalHandler = notifier
		decisionHandler = notifier
	}
	appServer := &http.Server{
		Addr: manager.Current().Listen,
		Handler: server.Application(server.Options{
			Store: st, Config: manager, Auth: authn, Revoker: revoker,
			Delivery: deliveryHandler, ApprovalNotifier: approvalHandler,
			DecisionNotifier: decisionHandler, Metrics: m, Logger: logger,
		}),
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
	go approval.NewSweeper(st, revoker, logger).Run(ctx)

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

// runMigrate applies the approval schema once and returns. It reads only
// DL_APPROVAL_DATABASE_DSN, so it needs neither the approval config nor the
// Lark/dl provider credentials.
func runMigrate(logger *log.Logger) {
	dsn := os.Getenv("DL_APPROVAL_DATABASE_DSN")
	if dsn == "" {
		logger.Fatal("DL_APPROVAL_DATABASE_DSN is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	st, err := store.Open(ctx, dsn)
	if err != nil {
		logger.Fatal(err)
	}
	defer st.Close()
	if err := st.Migrate(ctx); err != nil {
		logger.Fatal("approval schema migration failed")
	}
	logger.Print("approval schema ready")
}
