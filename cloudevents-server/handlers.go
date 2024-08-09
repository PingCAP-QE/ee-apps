package main

import (
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/tekton"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/testcaserun"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/tibuild"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/lark"
)

func indexHandler(c *gin.Context) {
	c.JSON(http.StatusOK, "Welcome to CloudEvents")
}

func healthzHandler(c *gin.Context) {
	c.String(http.StatusOK, "OK")
}

func newEventsHandlerFunc(cfg *config.Config) gin.HandlerFunc {
	p, err := cloudevents.NewHTTP()
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to create protocol")
	}

	handler, err := handler.NewEventProducer(cfg.Kafka)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create broker handler")
	}

	h, err := cloudevents.NewHTTPReceiveHandler(nil, p, handler.HandleCloudEvent)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create handler")
	}

	return func(c *gin.Context) {
		h.ServeHTTP(c.Writer, c.Request)
	}
}

// receiver creates a receiverFn wrapper class that is used by the client to
// validate and invoke the provided function.
func newCloudEventsHandler(cfg *config.Config) (handler.EventHandler, error) {
	caseRunHandler, err := testcaserun.NewHandler(cfg.Store)
	if err != nil {
		return nil, err
	}

	larkClient := lark.NewClient(cfg.Lark.AppID, cfg.Lark.AppSecret)
	tektonHandler, err := tekton.NewHandler(cfg.Tekton, larkClient)
	if err != nil {
		return nil, err
	}

	tibuildHandler, err := tibuild.NewHandler(cfg.TiBuild.ResultSinkURL, cfg.TiBuild.TriggerSinkURL)
	if err != nil {
		return nil, err
	}

	return new(handler.CompositeEventHandler).AddHandlers(
		caseRunHandler, tektonHandler, tibuildHandler,
	), nil
}
