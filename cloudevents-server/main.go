package main

import (
	"context"
	"flag"
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events"
)

func main() {
	var (
		ginMode    string
		serveAddr  string
		configFile string
	)

	flag.StringVar(&ginMode, "mode", gin.DebugMode, "server run mode")
	flag.StringVar(&serveAddr, "addr", "0.0.0.0:8080", "server run addr")
	flag.StringVar(&configFile, "config", "config.yaml", "Path to the config yaml file")
	flag.Parse()

	// Load cfg.
	var cfg config
	if err := cfg.LoadFromFile(configFile); err != nil {
		log.Fatal().Err(err).Msg("load config failed!")
	}
	dbClient, dbErr := newStoreClient(context.Background(), cfg)
	if dbErr != nil {
		log.Fatal().Err(dbErr).Msg("connect database failed!")
	}
	defer dbClient.Close()

	gin.SetMode(ginMode)
	switch ginMode {
	case gin.TestMode:
		zerolog.SetGlobalLevel(zerolog.TraceLevel)
	case gin.DebugMode:
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	default:
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	}

	gin.SetMode(ginMode)
	r := gin.Default()
	_ = r.SetTrustedProxies(nil)

	setRouters(r, dbClient)

	if err := http.ListenAndServe(serveAddr, r); err != nil {
		log.Fatal().Err(err).Send()
	}
}

func setRouters(r gin.IRoutes, dbClient *ent.Client) {
	r.GET("/", indexHandler)
	r.GET("/healthz", healthzHandler)
	r.POST("/events", eventsHandler(dbClient))
}

func indexHandler(c *gin.Context) {
	c.JSON(http.StatusOK, "Welcome to CloudEvents")
}

func healthzHandler(c *gin.Context) {
	c.String(http.StatusOK, "OK")
}

func eventsHandler(store *ent.Client) gin.HandlerFunc {
	p, err := cloudevents.NewHTTP()
	if err != nil {
		log.Fatal().
			Err(err).
			Msg("Failed to create protocol")
	}

	h, err := cloudevents.NewHTTPReceiveHandler(nil, p, events.NewEventsHandler(store))
	if err != nil {
		log.Fatal().
			Err(err).
			Msg("failed to create handler")
	}

	return func(c *gin.Context) {
		h.ServeHTTP(c.Writer, c.Request)
	}
}
