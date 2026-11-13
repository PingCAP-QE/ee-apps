package handler

import (
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
)

func eventOfType(t string) cloudevents.Event {
	event := cloudevents.NewEvent()
	event.SetType(t)
	return event
}

type stubHandler struct {
	types   []string
	panics  bool
	results []cloudevents.Result
	calls   *[]string
	name    string
}

func (s *stubHandler) SupportEventTypes() []string { return s.types }

func (s *stubHandler) Handle(cloudevents.Event) cloudevents.Result {
	if s.calls != nil {
		*s.calls = append(*s.calls, s.name)
	}
	if s.panics {
		panic("handler exploded")
	}
	if len(s.results) == 0 {
		return cloudevents.ResultACK
	}
	return s.results[0]
}

// A handler runs on the Kafka consumer goroutine, so a panic must not escape:
// one malformed event would otherwise stop every notification.
func TestCompositeHandlerRecoversFromPanic(t *testing.T) {
	var calls []string
	panicking := &stubHandler{types: []string{"t"}, panics: true, name: "panicking"}
	after := &stubHandler{types: []string{"t"}, name: "after", calls: &calls}

	h := (&CompositeEventHandler{}).AddHandlers(panicking, after)

	result := h.Handle(eventOfType("t"))

	if cloudevents.IsACK(result) {
		t.Fatal("a panicking handler must produce a failed receipt, not an ACK")
	}
	if len(calls) != 1 || calls[0] != "after" {
		t.Fatalf("later handlers must still run, got calls %v", calls)
	}
}

// A handler that returns normally keeps its own result.
func TestCompositeHandlerKeepsHandlerResult(t *testing.T) {
	h := (&CompositeEventHandler{}).AddHandlers(&stubHandler{types: []string{"t"}})

	if result := h.Handle(eventOfType("t")); !cloudevents.IsACK(result) {
		t.Fatalf("result = %v, want ACK", result)
	}
}
