package tekton

import (
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

func NewHandler(cfg config.Lark) (handler.EventHandler, error) {
	return &pipelineRunHandler{
		LarkClient:       newLarkClient(cfg),
		Receiver:         cfg.Receiver,
		RunDetailBaseURL: "https://do.pingcap.net/tekton",
	}, nil
}
