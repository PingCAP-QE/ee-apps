package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"

	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/events/handler"
)

type options struct {
	configPath  string
	debug       bool
	httpAddress string
}

// parseOptions parses the command line. Bot credentials are read from the
// configuration file only; there are intentionally no app-id/app-secret flags.
func parseOptions(args []string) (options, error) {
	var opts options
	var flagOutput bytes.Buffer
	fs := flag.NewFlagSet("chatops-lark", flag.ContinueOnError)
	fs.SetOutput(&flagOutput)
	fs.StringVar(&opts.configPath, "config", "config.yaml", "config yaml file")
	fs.BoolVar(&opts.debug, "debug", false, "debug mode")
	fs.StringVar(&opts.httpAddress, "http-addr", ":8080", "HTTP listen address for health checks")
	if err := fs.Parse(args); err != nil {
		usage := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(flagOutput.String()), err.Error()))
		if usage != "" {
			return options{}, fmt.Errorf("%w; %s", err, usage)
		}
		return options{}, err
	}
	return opts, nil
}

func main() {
	opts, err := parseOptions(os.Args[1:])
	if err != nil {
		log.Fatal().Err(err).Msg("Invalid command line options")
	}

	// Load configuration
	cfg, err := config.LoadConfig(opts.configPath)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load configuration")
	}

	if err := cfg.Validate(); err != nil {
		log.Fatal().Err(err).Msg("Invalid configuration")
	}

	// Override debug mode if specified via command line
	if opts.debug {
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
		log.Info().Msgf("Starting health check server on %s", opts.httpAddress)
		if err := http.ListenAndServe(opts.httpAddress, nil); err != nil {
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
