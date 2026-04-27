package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"

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
	// Create channel used by both the signal handler and server goroutines
	// to notify the main goroutine when to stop the server.
	errc := make(chan error)

	// Setup interrupt handler. This optional step configures the process so
	// that SIGINT and SIGTERM signals cause the services to stop gracefully.
	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGINT, syscall.SIGTERM)
		errc <- fmt.Errorf("%s", <-c)
	}()

	// Start http server
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal().Err(err).Msg("server error")
		}
	}()
	log.Info().Str("address", srv.Addr).Msg("server started.")

	// Start consumer workers
	cgWg := new(sync.WaitGroup)
	cgCtx, cgCancel := context.WithCancel(context.Background())
	cg.Start(cgCtx, cgWg)

	// Wait for signal.
	log.Warn().Msgf("exiting (%v)", <-errc)

	// Send cancellation signal to the goroutines.
	cgCancel()
	cgWg.Wait()
	log.Warn().Msg("Workers are gracefully stopped")

	// shutdown http server.
	if err := srv.Shutdown(context.Background()); err != nil {
		log.Error().Err(err).Msg("server shutdown error")
	}
	log.Warn().Msg("server gracefully stopped")
}

func setRouters(r gin.IRoutes, cfg *config.Config) {
	r.GET("/", indexHandler)
	r.GET("/healthz", healthzHandler)
	r.POST("/events", newEventsHandlerFunc(cfg))
}
