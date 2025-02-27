package main

import (
	"context"
	"flag"
	"net/http"
	"os"

	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
	"github.com/rs/zerolog/log"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/events/handler"
)

func main() {
	var (
		appID       = flag.String("app-id", "", "app id")
		appSecret   = flag.String("app-secret", "", "app secret")
		config      = flag.String("config", "config.yaml", "config yaml file")
		debugMode   = flag.Bool("debug", false, "debug mode")
		httpAddress = flag.String("http-addr", ":8080", "HTTP listen address for health checks")
	)
	flag.Parse()

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
	if *debugMode {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelDebug), lark.WithLogReqAtDebug(true))
	} else {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelInfo))
	}
	producerCli := lark.NewClient(*appID, *appSecret, producerOpts...)

	cfg := loadConfig(*config)

	eventHandler := dispatcher.NewEventDispatcher("", "").
		OnP2MessageReceiveV1(handler.NewRootForMessage(producerCli, cfg))

	consumerOpts := []larkws.ClientOption{larkws.WithEventHandler(eventHandler)}
	if *debugMode {
		consumerOpts = append(consumerOpts,
			larkws.WithLogLevel(larkcore.LogLevelDebug),
			larkws.WithAutoReconnect(true))
	}
	consumerOpts = append(consumerOpts, larkws.WithLogLevel(larkcore.LogLevelInfo))

	consumerCli := larkws.NewClient(*appID, *appSecret, consumerOpts...)
	// Now start the WebSocket client (blocking call)
	err := consumerCli.Start(context.Background())
	if err != nil {
		log.Fatal().Err(err).Msg("run failed for Lark WebSocket client")
	}
}

// loadConfig loads the YAML config into a map[string]any
func loadConfig(file string) map[string]any {
	f, err := os.Open(file)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to open config file")
	}
	defer f.Close()

	var cfg map[string]any
	err = yaml.NewDecoder(f).Decode(&cfg)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to decode config file")
	}
	return cfg
}
