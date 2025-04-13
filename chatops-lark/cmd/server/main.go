package main

import (
	"context"
	"flag"
	"net/http"

	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/botinfo"
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

	// Set default values and validate
	cfg.SetDefaults()
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

	// Set up Lark (Feishu) WebSocket client
	producerOpts := []lark.ClientOptionFunc{}
	if cfg.Debug {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelDebug), lark.WithLogReqAtDebug(true))
	} else {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelInfo))
	}
	producerCli := lark.NewClient(*appID, *appSecret, producerOpts...)

	// Get bot name at startup if not already in config
	if cfg.BotName == "" && *appID != "" && *appSecret != "" {
		botName, err := botinfo.GetBotName(context.Background(), *appID, *appSecret)
		if err != nil {
			log.Fatal().Err(err).Msg("Failed to get bot name from API. Please verify your App ID and App Secret, and ensure the bot is properly configured in the Lark platform. Alternatively, set a default bot name in the config.")
		} else if botName == "" {
			log.Fatal().Msg("Retrieved empty bot name from API. Please check your app configuration.")
		} else {
			log.Info().Str("botName", botName).Msg("Bot name retrieved from API successfully")
			// Store the bot name in the config
			cfg.BotName = botName
		}
	}

	eventHandler := dispatcher.NewEventDispatcher("", "").
		OnP2MessageReceiveV1(handler.NewRootForMessage(producerCli, cfg))

	consumerOpts := []larkws.ClientOption{larkws.WithEventHandler(eventHandler)}
	if cfg.Debug {
		consumerOpts = append(consumerOpts,
			larkws.WithLogLevel(larkcore.LogLevelDebug),
			larkws.WithAutoReconnect(true))
	}
	consumerOpts = append(consumerOpts, larkws.WithLogLevel(larkcore.LogLevelInfo))

	consumerCli := larkws.NewClient(*appID, *appSecret, consumerOpts...)
	// Now start the WebSocket client (blocking call)
	err = consumerCli.Start(context.Background())
	if err != nil {
		log.Fatal().Err(err).Msg("run failed for Lark WebSocket client")
	}
}
