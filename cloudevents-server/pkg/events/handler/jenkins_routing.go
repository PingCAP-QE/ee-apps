package handler

import (
	"strings"

	"github.com/rs/zerolog/log"
)

const jenkinsCloudEventTypePrefix = "dev.cdevents."

func (eb *EventProducer) resolveTopic(eventType string) (string, bool) {
	if topic, ok := eb.topicMapping[eventType]; ok {
		return topic, false
	}

	if strings.HasPrefix(eventType, jenkinsCloudEventTypePrefix) {
		return "", true
	}

	log.Debug().Str("event-type", eventType).Msg("No topic found for event type, using default topic")
	return eb.unknowEventTopic, false
}
