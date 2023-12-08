package tekton

import (
	"context"
	"net/http"
	"strings"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type Handler struct {
	LarkClient       *lark.Client
	RunDetailBaseURL string
	Receiver         string
}

func NewHandler(cfg config.Lark) (*Handler, error) {
	return &Handler{
		LarkClient:       newLarkClient(cfg),
		Receiver:         cfg.Receiver,
		RunDetailBaseURL: "https://do.pingcap.net/tekton",
	}, nil
}

func (h *Handler) SupportEventTypes() []string {
	return []string{
		string(tektoncloudevent.PipelineRunFailedEventV1),
		string(tektoncloudevent.PipelineRunRunningEventV1),
		string(tektoncloudevent.PipelineRunStartedEventV1),
		string(tektoncloudevent.PipelineRunSuccessfulEventV1),
		string(tektoncloudevent.PipelineRunUnknownEventV1),
		string(tektoncloudevent.RunFailedEventV1),
		string(tektoncloudevent.RunRunningEventV1),
		string(tektoncloudevent.RunStartedEventV1),
		string(tektoncloudevent.RunSuccessfulEventV1),
		string(tektoncloudevent.TaskRunFailedEventV1),
		string(tektoncloudevent.TaskRunRunningEventV1),
		string(tektoncloudevent.TaskRunStartedEventV1),
		string(tektoncloudevent.TaskRunSuccessfulEventV1),
		string(tektoncloudevent.TaskRunUnknownEventV1),
	}
}

func (h *Handler) Handle(event cloudevents.Event) cloudevents.Result {
	data := new(tektoncloudevent.TektonCloudEventData)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	if strings.HasPrefix(event.Type(), "dev.tekton.event.pipelinerun.") {
		return h.notifyRunStatus(event)
	}

	log.Debug().Str("ce-type", event.Type()).Msg("skip notifing for the event type.")
	return cloudevents.ResultACK
}

func (h *Handler) notifyRunStatus(event cloudevents.Event) cloudevents.Result {
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
