package tekton

import (
	"context"
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type pipelineRunHandler struct {
	LarkClient       *lark.Client
	RunDetailBaseURL string
	Receiver         string
}

func (h *pipelineRunHandler) SupportEventTypes() []string {
	return []string{
		string(tektoncloudevent.PipelineRunFailedEventV1),
		// string(tektoncloudevent.PipelineRunRunningEventV1),
		string(tektoncloudevent.PipelineRunStartedEventV1),
		string(tektoncloudevent.PipelineRunSuccessfulEventV1),
		// string(tektoncloudevent.PipelineRunUnknownEventV1),
	}
}

func (h *pipelineRunHandler) Handle(event cloudevents.Event) cloudevents.Result {
	data := new(tektoncloudevent.TektonCloudEventData)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	log.Debug().Str("ce-type", event.Type()).Msg("skip notifing for the event type.")
	return cloudevents.ResultACK
}

func (h *pipelineRunHandler) notifyRunStatus(event cloudevents.Event) cloudevents.Result {
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
