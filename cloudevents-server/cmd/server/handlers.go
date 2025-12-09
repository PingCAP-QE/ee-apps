package main

import (
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/testcaserun"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/tekton"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/tibuild"
)

func indexHandler(c *gin.Context) {
	c.JSON(http.StatusOK, "Welcome to CloudEvents")
}

func healthzHandler(c *gin.Context) {
	c.String(http.StatusOK, "OK")
}

func newEventsHandlerFunc(cfg *config.Config) gin.HandlerFunc {
	handler, err := handler.NewEventProducer(cfg.Kafka)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create broker handler")
	}

	return func(c *gin.Context) {
		p, err := cloudevents.NewHTTP()
		if err != nil {
			log.Fatal().Err(err).Msg("Failed to create protocol")
		}

		ceh, err := cloudevents.NewHTTPReceiveHandler(c, p, handler.HandleCloudEvent)
		if err != nil {
			log.Fatal().Err(err).Msg("failed to create handler")
		}

		ceh.ServeHTTP(c.Writer, c.Request)
	}
}

// receiver creates a receiverFn wrapper class that is used by the client to
// validate and invoke the provided function.
func newCloudEventsHandler(cfg *config.Config) (handler.EventHandler, error) {
	ret := new(handler.CompositeEventHandler)

	// Register test case run handler
	if cfg.TestCaseRun != nil {
		handler, err := testcaserun.NewHandler(cfg.TestCaseRun.Store)
		if err != nil {
			return nil, err
		}

		ret.AddHandlers(handler)
	}

	// Register Tekton pipelinerun and taskrun handlers
	if cfg.Tekton != nil {
		handler, err := tekton.NewHandler(*cfg.Tekton)
		if err != nil {
			return nil, err
		}
		ret.AddHandlers(handler)
	}

	// Register TiDB build handlers
	if cfg.TiBuild != nil {
		handler, err := tibuild.NewHandler(*cfg.TiBuild)
		if err != nil {
			return nil, err
		}
		ret.AddHandlers(handler)
	}

	return ret, nil
}
