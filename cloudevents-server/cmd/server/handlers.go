package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
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

const jenkinsEventTopic = "jenkins-event"
const jenkinsPipelineRunFinishedEventType = "dev.cdevents.pipelinerun.finished.0.1.0"

type cloudEventProducer interface {
	HandleCloudEvent(context.Context, cloudevents.Event) cloudevents.Result
	HandleCloudEventWithTopic(context.Context, cloudevents.Event, string) cloudevents.Result
}

func indexHandler(c *gin.Context) {
	c.JSON(http.StatusOK, "Welcome to CloudEvents")
}

func healthzHandler(c *gin.Context) {
	c.String(http.StatusOK, "OK")
}

func newEventsHandlerFunc(cfg *config.Config) gin.HandlerFunc {
	producer, err := handler.NewEventProducer(cfg.Kafka)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create broker handler")
	}

	return newStructuredEventsHandlerFunc(producer)
}

func newJenkinsEventsHandlerFunc(cfg *config.Config) gin.HandlerFunc {
	producer, err := handler.NewEventProducer(cfg.Kafka)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create broker handler")
	}

	return newJenkinsSinkHandlerFunc(producer)
}

func newStructuredEventsHandlerFunc(producer cloudEventProducer) gin.HandlerFunc {
	p, err := cloudevents.NewHTTP()
	if err != nil {
		log.Error().Err(err).Msg("failed to create cloudevents protocol")
		return func(c *gin.Context) {
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "failed to create protocol"})
		}
	}

	return func(c *gin.Context) {
		ceh, err := cloudevents.NewHTTPReceiveHandler(c, p, producer.HandleCloudEvent)
		if err != nil {
			log.Error().Err(err).Msg("failed to create cloudevents receive handler")
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "failed to create handler"})
			return
		}

		ceh.ServeHTTP(c.Writer, c.Request)
	}
}

func newJenkinsSinkHandlerFunc(producer cloudEventProducer) gin.HandlerFunc {
	return func(c *gin.Context) {
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			log.Warn().Err(err).Msg("failed to read jenkins event payload")
			c.JSON(http.StatusBadRequest, gin.H{"status": "invalid", "error": "failed to read request body"})
			return
		}

		event, err := parseJenkinsPluginCloudEvent(body)
		if err != nil {
			log.Warn().Err(err).Str("payload", truncateLogPayload(string(body))).Msg("failed to parse jenkins event payload")
			c.JSON(http.StatusBadRequest, gin.H{"status": "invalid", "error": err.Error()})
			return
		}

		if event.Type() != jenkinsPipelineRunFinishedEventType {
			log.Debug().
				Str("ce-id", event.ID()).
				Str("ce-type", event.Type()).
				Msg("ignoring unsupported jenkins event type")
			c.JSON(http.StatusOK, gin.H{
				"status": "ignored",
				"id":     event.ID(),
				"type":   event.Type(),
			})
			return
		}

		result := producer.HandleCloudEventWithTopic(c.Request.Context(), event, jenkinsEventTopic)
		if !cloudevents.IsACK(result) {
			log.Warn().Str("ce-id", event.ID()).Str("ce-type", event.Type()).Err(result).Msg("failed to enqueue jenkins cloud event")
			c.JSON(http.StatusBadGateway, gin.H{"status": "failed", "error": result.Error()})
			return
		}

		c.JSON(http.StatusOK, gin.H{
			"status": "accepted",
			"id":     event.ID(),
			"type":   event.Type(),
		})
	}
}

func parseJenkinsPluginCloudEvent(body []byte) (cloudevents.Event, error) {
	return parseStructuredJenkinsCloudEvent(body)
}

func parseStructuredJenkinsCloudEvent(body []byte) (cloudevents.Event, error) {
	var event cloudevents.Event
	if err := json.Unmarshal(body, &event); err != nil {
		return cloudevents.Event{}, fmt.Errorf("decode structured jenkins cloud event json: %w", err)
	}
	if err := event.Validate(); err != nil {
		return cloudevents.Event{}, fmt.Errorf("validate structured jenkins cloud event: %w", err)
	}
	return event, nil
}

func truncateLogPayload(payload string) string {
	const maxLen = 512
	if len(payload) <= maxLen {
		return payload
	}
	return payload[:maxLen] + "...(truncated)"
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
