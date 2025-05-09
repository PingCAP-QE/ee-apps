package impl

import (
	cloudevents "github.com/cloudevents/sdk-go/v2"
)

// Worker provides handling for cloud events.
type Worker interface {
	Handle(event cloudevents.Event) cloudevents.Result
}
