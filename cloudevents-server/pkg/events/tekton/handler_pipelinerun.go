package tekton

import (
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

const (
	defaultReceiversKey = "*"
)

type pipelineRunHandler struct {
	config.Tekton
	LarkClient *lark.Client
}

type AnnotationsGetter interface {
	GetAnnotations() map[string]string
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
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	handlerLog := log.With().Stack().Caller().
		Str("ce-type", event.Type()).
		Str("ce-id", event.ID()).
		Logger()

	switch tektoncloudevent.TektonEventType(event.Type()) {
	case tektoncloudevent.PipelineRunStartedEventV1,
		tektoncloudevent.PipelineRunFailedEventV1,
		tektoncloudevent.PipelineRunSuccessfulEventV1:
		var receivers []string
		// send notify to the trigger user if it's existed, else send to the receivers configurated by type.
		if receiver := getTriggerUser(data.PipelineRun); receiver != "" {
			receivers = []string{receiver}
		} else {
			receivers = getReceivers(event, h.Notifications)
		}
		if len(receivers) == 0 {
			return cloudevents.ResultACK
		}

		infos, err := extractLarkInfosFromEvent(event, h.DashboardBaseURL, h.FailedStepTailLines)
		if err != nil {
			handlerLog.Err(err).
				Bytes("ce-data", event.Data()).
				Str("receivers", strings.Join(receivers, ",")).
				Msg("failed to extract lark infos from event")
			return cloudevents.ResultNACK
		}

		handlerLog.Debug().
			Str("receivers", strings.Join(receivers, ",")).
			Msg("send notification for the event type.")
		return composeAndSendLarkMessages(h.LarkClient, receivers, infos)
	default:
		handlerLog.Debug().Msg("skip notifing for the event type.")
		return cloudevents.ResultACK
	}
}
