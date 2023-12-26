package tekton

import (
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

func NewHandler(cfg config.Lark) (handler.EventHandler, error) {
	larkClient := newLarkClient(cfg)
	baseURL := "https://do.pingcap.net/tekton"
	ret := new(handler.CompositeEventHandler).AddHandlers(
		&pipelineRunHandler{LarkClient: larkClient, Receiver: cfg.Receiver, RunDetailBaseURL: baseURL},
		&taskRunHandler{LarkClient: larkClient, Receiver: cfg.Receiver, RunDetailBaseURL: baseURL},
	)

	return ret, nil
}
