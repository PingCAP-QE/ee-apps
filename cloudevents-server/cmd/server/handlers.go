package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/testcaserun"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/tekton"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/tibuild"
)

var jenkinsPluginCloudEventRegexp = regexp.MustCompile(`^CloudEvent\{id='([^']+)', source=([^,]+), type='([^']+)', datacontenttype='([^']*)', time=([^,]+), data=BytesCloudEventData\{value=\[([^\]]*)\]\}, extensions=\{.*\}\}$`)

const jenkinsEventTopic = "jenkins-event"

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

func newEventsHandlerFunc(producer cloudEventProducer) gin.HandlerFunc {
	return newStructuredEventsHandlerFunc(producer)
}

func newJenkinsEventsHandlerFunc(producer cloudEventProducer) gin.HandlerFunc {
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
	raw := strings.TrimSpace(string(body))
	matches := jenkinsPluginCloudEventRegexp.FindStringSubmatch(raw)
	if matches == nil {
		return cloudevents.Event{}, fmt.Errorf("payload does not match Jenkins CD Events HTTP sink format")
	}

	payloadBytes, err := parseSignedByteArray(matches[6])
	if err != nil {
		return cloudevents.Event{}, fmt.Errorf("parse jenkins event data bytes: %w", err)
	}

	event := cloudevents.NewEvent()
	event.SetID(matches[1])
	event.SetSource(matches[2])
	event.SetType(matches[3])

	if matches[4] != "" {
		event.SetDataContentType(matches[4])
	}

	if matches[5] != "" {
		eventTime, err := time.Parse(time.RFC3339Nano, matches[5])
		if err != nil {
			return cloudevents.Event{}, fmt.Errorf("parse jenkins event time: %w", err)
		}
		event.SetTime(eventTime)
	}

	if len(payloadBytes) > 0 {
		if strings.Contains(strings.ToLower(matches[4]), "json") {
			var data any
			if err := json.Unmarshal(payloadBytes, &data); err != nil {
				return cloudevents.Event{}, fmt.Errorf("decode jenkins event data json: %w", err)
			}
			if err := event.SetData(matches[4], data); err != nil {
				return cloudevents.Event{}, fmt.Errorf("set jenkins event data: %w", err)
			}
		} else {
			if err := event.SetData(matches[4], payloadBytes); err != nil {
				return cloudevents.Event{}, fmt.Errorf("set jenkins event data: %w", err)
			}
		}
	}

	if err := event.Validate(); err != nil {
		return cloudevents.Event{}, fmt.Errorf("validate jenkins cloud event: %w", err)
	}

	return event, nil
}

func parseSignedByteArray(raw string) ([]byte, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}

	parts := strings.Split(raw, ",")
	buf := make([]byte, 0, len(parts))
	for _, part := range parts {
		value, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil {
			return nil, err
		}
		if value < -128 || value > 255 {
			return nil, fmt.Errorf("byte value %d out of range", value)
		}
		if value < 0 {
			value += 256
		}
		buf = append(buf, byte(value))
	}

	return buf, nil
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
