package events

import (
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/tekton"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/testcaserun"
)

type EventHandler interface {
	// Valid fn signatures are:
	// * func()
	// * func() protocol.Result
	// * func(context.Context)
	// * func(context.Context) protocol.Result
	// * func(event.Event)
	// * func(event.Event) transport.Result
	// * func(context.Context, event.Event)
	// * func(context.Context, event.Event) protocol.Result
	// * func(event.Event) *event.Event
	// * func(event.Event) (*event.Event, protocol.Result)
	// * func(context.Context, event.Event) *event.Event
	// * func(context.Context, event.Event) (*event.Event, protocol.Result)
	Handle(event cloudevents.Event) cloudevents.Result
	SupportEventTypes() []string
}

type handlerImpl struct {
	handleMap map[string]EventHandler
}

func (h *handlerImpl) SupportEventTypes() []string {
	var ret []string
	for t, _ := range h.handleMap {
		ret = append(ret, t)
	}

	return ret
}

func (h *handlerImpl) Handle(event cloudevents.Event) cloudevents.Result {
	eh, ok := h.handleMap[event.Type()]
	if ok {
		return eh.Handle(event)
	}

	log.Error().Str("type", event.Type()).Msg("none handlers registered")
	return cloudevents.NewHTTPResult(http.StatusNotFound, "none handlers registered for event type: %s, ignore it.", event.Type())
}

func (h *handlerImpl) addChildHandlers(handlers ...EventHandler) *handlerImpl {
	if h.handleMap == nil {
		h.handleMap = make(map[string]EventHandler)
	}

	for _, e := range handlers {
		for _, eventType := range e.SupportEventTypes() {
			h.handleMap[eventType] = e
		}
	}

	return h
}

// receiver creates a receiverFn wrapper class that is used by the client to
// validate and invoke the provided function.
func NewEventsHandler(cfg *config.Config) (EventHandler, error) {
	caseRunHandler, err := testcaserun.NewHandler(cfg.Store)
	if err != nil {
		return nil, err
	}

	tektonHandler, err := tekton.NewHandler(cfg.Lark)
	if err != nil {
		return nil, err
	}

	ret := new(handlerImpl)
	ret.addChildHandlers(caseRunHandler, tektonHandler)

	return ret, nil
}
