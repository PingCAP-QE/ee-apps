package handler

import cloudevents "github.com/cloudevents/sdk-go/v2"

// EventHandler is an interface that defines the Handle method.
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
	// Handle handles the given event.
	Handle(event cloudevents.Event) cloudevents.Result
	// SupportEventTypes returns a list of supported event types.
	SupportEventTypes() []string
}
