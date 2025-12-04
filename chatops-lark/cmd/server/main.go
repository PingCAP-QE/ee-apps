package main

import (
	"context"
	"flag"
	"net/http"

	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/events/handler"
)

func main() {
	var (
		appID       = flag.String("app-id", "", "app id")
		appSecret   = flag.String("app-secret", "", "app secret")
		configPath  = flag.String("config", "config.yaml", "config yaml file")
		debugMode   = flag.Bool("debug", false, "debug mode")
		httpAddress = flag.String("http-addr", ":8080", "HTTP listen address for health checks")
	)
	flag.Parse()

	// Load configuration
	cfg, err := config.LoadConfig(*configPath)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load configuration")
	}

	// The priority of the CLI options is higher than the configuration file.
	if *appID != "" {
		cfg.AppID = *appID
	}
	if *appSecret != "" {
		cfg.AppSecret = *appSecret
	}

	if err := cfg.Validate(); err != nil {
		log.Fatal().Err(err).Msg("Invalid configuration")
	}

	// Override debug mode if specified via command line
	if *debugMode {
		cfg.Debug = true
	}

	// Start the HTTP server in a separate goroutine
	go func() {
		http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("OK"))
		})
		http.HandleFunc("/livez", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("OK"))
		})
		log.Info().Msgf("Starting health check server on %s", *httpAddress)
		if err := http.ListenAndServe(*httpAddress, nil); err != nil {
			log.Fatal().Err(err).Msg("Failed to start health check server")
		}
	}()

	eventHandler := dispatcher.NewEventDispatcher("", "").
		OnP2MessageReceiveV1(handler.NewRootForMessage(cfg))

	consumerOpts := []larkws.ClientOption{larkws.WithEventHandler(eventHandler)}
	if cfg.Debug {
		consumerOpts = append(consumerOpts,
			larkws.WithLogLevel(larkcore.LogLevelDebug),
			larkws.WithAutoReconnect(true))
	}
	consumerOpts = append(consumerOpts, larkws.WithLogLevel(larkcore.LogLevelInfo))

	consumerCli := larkws.NewClient(cfg.AppID, cfg.AppSecret, consumerOpts...)
	// Now start the WebSocket client (blocking call)
	err = consumerCli.Start(context.Background())
	if err != nil {
		log.Fatal().Err(err).Msg("run failed for Lark WebSocket client")
	}
}
