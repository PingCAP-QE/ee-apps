package tekton

import (
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

type taskRunHandler struct {
	config.Tekton
	LarkClient *lark.Client
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
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	handlerLog := log.With().Stack().Caller().
		Str("ce-type", event.Type()).
		Str("ce-id", event.ID()).
		Logger()

	switch event.Type() {
	case string(tektoncloudevent.TaskRunFailedEventV1):
		// Skip notify the taskrun when it created by a pipelineRun.
		if data.TaskRun.Labels[pipeline.PipelineRunLabelKey] != "" {
			break
		}

		var receivers []string
		// send notify to the trigger user if it's existed, else send to the receivers configurated by type.
		if receiver := getTriggerUser(data.TaskRun); receiver != "" {
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
				Msg("failed to extract lark infos")
			return err
		}

		handlerLog.Debug().
			Str("receivers", strings.Join(receivers, ",")).
			Msg("send notification for the event type.")
		return composeAndSendLarkMessages(h.LarkClient, receivers, infos)
	}

	handlerLog.Debug().Msg("skip notifing for the event type.")
	return cloudevents.ResultACK
}
