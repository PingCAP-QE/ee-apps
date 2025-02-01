package main

import (
	"context"
	"flag"
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
		appID     = flag.String("app-id", "", "app id")
		appSecret = flag.String("app-secret", "", "app secret")
		config    = flag.String("config", "config.yaml", "config yaml file")
		debugMode = flag.Bool("debug", false, "debug mode")
	)
	flag.Parse()

	producerOpts := []lark.ClientOptionFunc{}
	if *debugMode {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelDebug), lark.WithLogReqAtDebug(true))
	}
	producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelInfo))
	producerCli := lark.NewClient(*appID, *appSecret, producerOpts...)

	eventHandler := dispatcher.NewEventDispatcher("", "").
		OnP2MessageReceiveV1(handler.NewRootForMessage(producerCli, loadConfig(*config)))
	consumerOpts := []larkws.ClientOption{larkws.WithEventHandler(eventHandler)}
	if *debugMode {
		consumerOpts = append(consumerOpts,
			larkws.WithLogLevel(larkcore.LogLevelDebug),
			larkws.WithAutoReconnect(true))
	}
	consumerOpts = append(consumerOpts, larkws.WithLogLevel(larkcore.LogLevelInfo))

	consumerCli := larkws.NewClient(*appID, *appSecret, consumerOpts...)
	err := consumerCli.Start(context.Background())
	if err != nil {
		log.Fatal().Err(err).Msg("run failed")
	}
}

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
