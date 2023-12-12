package main

import (
	"flag"
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
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
	r := gin.Default()
	_ = r.SetTrustedProxies(nil)

	setRouters(r, cfg)
	log.Info().Str("address", serveAddr).Msg("server started.")

	if err := http.ListenAndServe(serveAddr, r); err != nil {
		log.Fatal().Err(err).Send()
	}
}

func setRouters(r gin.IRoutes, cfg *config.Config) {
	r.GET("/", indexHandler)
	r.GET("/healthz", healthzHandler)
	r.POST("/events", newEventsHandlerFunc(cfg))
}
