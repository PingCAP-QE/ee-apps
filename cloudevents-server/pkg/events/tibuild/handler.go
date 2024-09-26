package tibuild

import (
	"context"
	"fmt"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/client"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

// NewHandler creates a new SinkHandler with the specified sink URL.
func NewHandler(tibuildSinkURL, tektonSinkURL string) (handler.EventHandler, error) {
	routeToTbHandler, err := newRouteHandler(tibuildSinkURL)
	if err != nil {
		return nil, err
	}
	routeToTektonHandler, err := newRouteHandler(tektonSinkURL)
	if err != nil {
		return nil, err
	}

	ret := new(handler.CompositeEventHandler).AddHandlers(
		&triggerHandler{routeToTektonHandler},
		&resultHandler{routeToTbHandler},
	)

	return ret, nil
}

type triggerHandler struct{ *routeHandler }

// SupportEventTypes returns an empty list, indicating that this handler supports all event types.
func (h *triggerHandler) SupportEventTypes() []string {
	return []string{
		EventTypeDevbuildFakeGithubCreate,
		EventTypeDevbuildFakeGithubPush,
		EventTypeDevbuildFakeGithubPullRequest,
		EventTypeHotfixFakeGithubCreate,
		EventTypeHotfixFakeGithubPush,
		EventTypeHotfixFakeGithubPullRequest,
	}
}

type resultHandler struct{ *routeHandler }

func (h *resultHandler) SupportEventTypes() []string {
	return []string{
		string(tektoncloudevent.PipelineRunFailedEventV1),
		string(tektoncloudevent.PipelineRunRunningEventV1),
		string(tektoncloudevent.PipelineRunStartedEventV1),
		string(tektoncloudevent.PipelineRunSuccessfulEventV1),
	}
}

// routeHandler is an event handler that forward events to target sink URL using the CloudEvents SDK.
type routeHandler struct {
	sinkURL string // tibuild's sink URL.
	client  client.Client
}

// Handle routes the given event to the specified sink URL using the CloudEvents SDK.
func (h *routeHandler) Handle(event cloudevents.Event) cloudevents.Result {
	log.Debug().
		Str("sink-url", h.sinkURL).
		Str("ce-type", event.Type()).
		Any("detail", event).
		Msg("Send to sinker")
	return h.client.Send(context.Background(), event)
}

func newRouteHandler(sinkURL string) (*routeHandler, error) {
	client, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(sinkURL))
	if err != nil {
		return nil, fmt.Errorf("error creating client: %v", err)
	}

	return &routeHandler{client: client, sinkURL: sinkURL}, nil
}
