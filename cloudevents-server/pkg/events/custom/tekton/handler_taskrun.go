package tekton

import (
	"context"
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type taskRunHandler struct {
	LarkClient       *lark.Client
	RunDetailBaseURL string
	Receiver         string
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
		return h.notifyRunStatus(event)
	default:
		log.Debug().Str("ce-type", event.Type()).Msg("skip notifing for the event type.")
		return cloudevents.ResultACK
	}
}

func (h *taskRunHandler) notifyRunStatus(event cloudevents.Event) cloudevents.Result {
	createMsgReq, err := newLarkMessage(h.Receiver, event, h.RunDetailBaseURL)
	if err != nil {
		log.Error().Err(err).Msg("compose lark message failed")
		return cloudevents.NewHTTPResult(http.StatusInternalServerError, "compose lark message failed: %v", err)
	}

	resp, err := h.LarkClient.Im.Message.Create(context.Background(), createMsgReq)
	if err != nil {
		log.Error().Err(err).Msg("send lark message failed")
		return cloudevents.NewHTTPResult(http.StatusInternalServerError, "send lark message failed: %v", err)
	}

	if resp.Success() {
		log.Info().
			Str("request-id", resp.RequestId()).
			Str("message-id", *resp.Data.MessageId).
			Msg("send lark message successfully.")
		return cloudevents.ResultACK
	}

	log.Error().Err(resp).Msg("send lark message failed!")
	return cloudevents.NewHTTPResult(http.StatusInternalServerError, "send lark message failed!")
}
