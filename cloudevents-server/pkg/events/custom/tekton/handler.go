package tekton

import (
	"encoding/json"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

const (
	eventContextAnnotationKey          = "tekton.dev/ce-context"
	eventContextAnnotationInnerKeyUser = "user"
)

func NewHandler(cfg config.Tekton, larkClient *larksdk.Client) (handler.EventHandler, error) {
	ret := new(handler.CompositeEventHandler).AddHandlers(
		&pipelineRunHandler{LarkClient: larkClient, Tekton: cfg},
		&taskRunHandler{LarkClient: larkClient, Tekton: cfg},
	)

	return ret, nil
}

func getTriggerUser(run AnnotationsGetter) string {
	eventContext := run.GetAnnotations()[eventContextAnnotationKey]
	if eventContext == "" {
		return ""
	}

	contextData := make(map[string]string)
	if err := json.Unmarshal([]byte(eventContext), &contextData); err != nil {
		return ""
	}

	return contextData[eventContextAnnotationInnerKeyUser]
}
