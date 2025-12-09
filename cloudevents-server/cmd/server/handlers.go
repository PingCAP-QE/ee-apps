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
	type handlerInitFunc func() (handler.EventHandler, error)
	var inits = []handlerInitFunc{
		// testcase run handler
		func() (handler.EventHandler, error) {
			if cfg.TestCaseRun == nil {
				return nil, nil
			}
			return testcaserun.NewHandler(cfg.TestCaseRun.Store)
		},
		// tekton runs handler
		func() (handler.EventHandler, error) {
			if cfg.Tekton == nil {
				return nil, nil
			}
			return tekton.NewHandler(*cfg.Tekton)
		},
		// tibuild handler
		func() (handler.EventHandler, error) {
			if cfg.TiBuild == nil {
				return nil, nil
			}
			return tibuild.NewHandler(*cfg.TiBuild)
		},
	}

	ret := new(handler.CompositeEventHandler)
	for _, initFn := range inits {
		h, err := initFn()
		if err != nil {
			return nil, err
		}
		if h != nil {
			ret.AddHandlers(h)
		}
	}

	return ret, nil
}
