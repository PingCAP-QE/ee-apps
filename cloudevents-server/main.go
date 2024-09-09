package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

func main() {
	var (
		ginMode    string
		serveAddr  string
		configFile string
	)

	flag.StringVar(&ginMode, "mode", gin.DebugMode, "server run mode")
	flag.StringVar(&serveAddr, "addr", "0.0.0.0:80", "server run addr")
	flag.StringVar(&configFile, "config", "config.yaml", "Path to the config yaml file")
	flag.Parse()

	// Load cfg.
	cfg := new(config.Config)
	if err := cfg.LoadFromFile(configFile); err != nil {
		log.Fatal().Err(err).Msg("load config failed!")
	}

	gin.SetMode(ginMode)
	switch ginMode {
	case gin.TestMode:
		zerolog.SetGlobalLevel(zerolog.TraceLevel)
	case gin.DebugMode:
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	default:
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}

	gin.SetMode(ginMode)
	ginEngine := gin.Default()
	_ = ginEngine.SetTrustedProxies(nil)

	setRouters(ginEngine, cfg)

	hd, err := newCloudEventsHandler(cfg)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create cloudevents handler")
	}
	log.Debug().Any("types", hd.SupportEventTypes()).Msgf("registered event handlers")

	cg, err := handler.NewEventConsumerGroup(cfg.Kafka, hd)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create consumer group")
	}

	srv := &http.Server{Addr: serveAddr, Handler: ginEngine}
	startServices(srv, cg)
}

func startServices(srv *http.Server, cg handler.EventConsumerGroup) {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal().Err(err).Msg("server error")
		}
	}()
	log.Info().Str("address", srv.Addr).Msg("server started.")
	cgCtx, cgCancel := context.WithCancel(context.Background())
	go cg.Start(cgCtx)

	// Wait for interrupt signal to gracefully shutdown
	sig := <-sigChan
	log.Warn().Str("signal", sig.String()).Msg("signal received")

	// shutdown consumer group.
	cgCancel()

	// shutdown http server.
	shutdownSrvCtx, shutdownSrvCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownSrvCancel()
	if err := srv.Shutdown(shutdownSrvCtx); err != nil {
		log.Error().Err(err).Msg("server shutdown error")
	}

	log.Info().Msg("server gracefully stopped")
}

func setRouters(r gin.IRoutes, cfg *config.Config) {
	r.GET("/", indexHandler)
	r.GET("/healthz", healthzHandler)
	r.POST("/events", newEventsHandlerFunc(cfg))
}
