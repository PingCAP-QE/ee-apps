package tekton

import (
	"encoding/json"
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

const (
	eventContextAnnotationKey          = "tekton.dev/ce-context"
	eventContextAnnotationInnerKeyUser = "user"
)

type pipelineRunHandler struct {
	LarkClient       *lark.Client
	RunDetailBaseURL string
	Receivers        []string
}

func (h *pipelineRunHandler) SupportEventTypes() []string {
	return []string{
		string(tektoncloudevent.PipelineRunFailedEventV1),
		string(tektoncloudevent.PipelineRunRunningEventV1),
		string(tektoncloudevent.PipelineRunStartedEventV1),
		string(tektoncloudevent.PipelineRunSuccessfulEventV1),
		string(tektoncloudevent.PipelineRunUnknownEventV1),
	}
}

func (h *pipelineRunHandler) Handle(event cloudevents.Event) cloudevents.Result {
	data := new(tektoncloudevent.TektonCloudEventData)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	switch tektoncloudevent.TektonEventType(event.Type()) {
	case tektoncloudevent.PipelineRunStartedEventV1,
		tektoncloudevent.PipelineRunFailedEventV1,
		tektoncloudevent.PipelineRunSuccessfulEventV1:
		receivers := h.Receivers
		if receiver := getTriggerUser(data.PipelineRun); receiver != "" {
			receivers = append(receivers, receiver)
		}

		return sendLarkMessages(h.LarkClient, receivers, event, h.RunDetailBaseURL)
	default:
		log.Debug().Str("ce-type", event.Type()).Msg("skip notifing for the event type.")
		return cloudevents.ResultACK
	}
}

func getTriggerUser(pr *v1beta1.PipelineRun) string {
	eventContext := pr.Annotations[eventContextAnnotationKey]
	if eventContext == "" {
		return ""
	}

	contextData := make(map[string]string)
	if err := json.Unmarshal([]byte(eventContext), &contextData); err != nil {
		return ""
	}

	return contextData[eventContextAnnotationInnerKeyUser]
}
