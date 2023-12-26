package tekton

import (
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type taskRunHandler struct {
	LarkClient       *lark.Client
	RunDetailBaseURL string
	Receivers        []string
}

func (h *taskRunHandler) SupportEventTypes() []string {
	return []string{
		string(tektoncloudevent.TaskRunFailedEventV1),
		string(tektoncloudevent.TaskRunRunningEventV1),
		string(tektoncloudevent.TaskRunStartedEventV1),
		string(tektoncloudevent.TaskRunSuccessfulEventV1),
		string(tektoncloudevent.TaskRunUnknownEventV1),
	}
}

func (h *taskRunHandler) Handle(event cloudevents.Event) cloudevents.Result {
	data := new(tektoncloudevent.TektonCloudEventData)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	switch event.Type() {
	case string(tektoncloudevent.TaskRunFailedEventV1):
		return sendLarkMessages(h.LarkClient, h.Receivers, event, h.RunDetailBaseURL)
	default:
		log.Debug().Str("ce-type", event.Type()).Msg("skip notifing for the event type.")
		return cloudevents.ResultACK
	}
}
