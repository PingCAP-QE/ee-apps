package handler

import (
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/pkg/errors"
	"github.com/rs/zerolog/log"
)

// CompositeEventHandler is a public struct that composes multiple event handlers.
type CompositeEventHandler struct {
	handleMap map[string][]EventHandler
}

// SupportEventTypes returns a list of supported event types.
func (h *CompositeEventHandler) SupportEventTypes() []string {
	var ret []string
	for t := range h.handleMap {
		ret = append(ret, t)
	}

	return ret
}

// Handle handles the given event.
func (h *CompositeEventHandler) Handle(event cloudevents.Event) cloudevents.Result {
	handlers, ok := h.handleMap[event.Type()]
	if !ok {
		log.Warn().Str("ce-type", event.Type()).Msg("no handlers registered for the event")
		return cloudevents.NewReceipt(false, "no handlers registered for the event")
	}

	var results []cloudevents.Result

	// Loop through all registered handlers for the given event type.
	//
	// A handler runs on the Kafka consumer goroutine, so a panic there takes the
	// whole process down: one malformed event would stop every notification.
	// Recover per handler and turn it into a failed receipt, which the consumer
	// records and moves past.
	for _, eh := range handlers {
		handlerResult := handleSafely(eh, event)
		if !cloudevents.IsACK(handlerResult) {
			// Accumulate errors from each handler.
			results = append(results, handlerResult)
		}
	}

	// Combine and return the aggregated results.
	return combineResults(results)
}

// handleSafely runs one handler and converts a panic into a failed receipt.
func handleSafely(eh EventHandler, event cloudevents.Event) (result cloudevents.Result) {
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Error().
				Interface("panic", recovered).
				Str("ce-type", event.Type()).
				Str("ce-id", event.ID()).
				Msg("event handler panicked")
			result = cloudevents.NewReceipt(false, "event handler panicked: %v", recovered)
		}
	}()
	return eh.Handle(event)
}

// AddHandlers adds child handlers to the composite event handler.
func (h *CompositeEventHandler) AddHandlers(handlers ...EventHandler) *CompositeEventHandler {
	if h.handleMap == nil {
		h.handleMap = make(map[string][]EventHandler)
	}

	for _, e := range handlers {
		for _, eventType := range e.SupportEventTypes() {
			h.handleMap[eventType] = append(h.handleMap[eventType], e)
		}
	}

	return h
}

// combineResults combines multiple errors into a single error.
func combineResults(errorsList []cloudevents.Result) cloudevents.Result {
	// If there are no errors, return nil.
	if len(errorsList) == 0 {
		return cloudevents.ResultACK
	}

	// Combine errors using the first error as the base.
	result := errorsList[0]

	// Append additional errors, if any, to the base error.
	for i := 1; i < len(errorsList); i++ {
		result = errors.Wrap(result, errorsList[i].Error())
	}

	return result
}
