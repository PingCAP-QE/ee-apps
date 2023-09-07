package events

import (
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/custom/testcaserun"
)

type eventHandler func(event cloudevents.Event) cloudevents.Result

// receiver creates a receiverFn wrapper class that is used by the client to
// validate and invoke the provided function.
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
func NewEventsHandler(store *ent.Client) eventHandler {
	caserunHandler := &testcaserun.Handler{Storage: store.ProblemCaseRun}

	handleMap := map[string]eventHandler{
		custom.EventTypeTestCaseRunReport: caserunHandler.Handle,
	}

	return func(event cloudevents.Event) cloudevents.Result {
		log.Debug().Any("event", event).Msg("received event")
		eh, ok := handleMap[event.Type()]
		if ok {
			return eh(event)
		}

		return cloudevents.NewHTTPResult(http.StatusNotFound, "none handlers registered for event type: %s, ignore it.", event.Type())
	}
}
