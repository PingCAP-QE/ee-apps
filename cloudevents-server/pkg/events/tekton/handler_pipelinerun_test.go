package tekton

import (
	"encoding/json"
	"reflect"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"

	_ "embed"
)

// test events.
var (
	//go:embed testdata/event-pipelinerun.failed.json
	pipelineRunFailedEventBytes []byte
	//go:embed testdata/event-pipelinerun.running.json
	pipelineRunRunningEventBytes []byte
	//go:embed testdata/event-pipelinerun.started.json
	pipelineRunStartedEventBytes []byte
	//go:embed testdata/event-pipelinerun.successful.json
	pipelineRunSuccessfulEventBytes []byte
	//go:embed testdata/event-pipelinerun.unknown.json
	pipelineRunUnknownEventBytes []byte
)

func Test_pipelineRunHandler_Handle(t *testing.T) {
	type fields struct {
		LarkClient *lark.Client
	}
	type args struct {
	}
	tests := []struct {
		name      tektoncloudevent.TektonEventType
		eventJSON []byte
		want      cloudevents.Result
	}{
		{name: tektoncloudevent.PipelineRunFailedEventV1, eventJSON: pipelineRunFailedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.PipelineRunRunningEventV1, eventJSON: pipelineRunRunningEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.PipelineRunStartedEventV1, eventJSON: pipelineRunStartedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.PipelineRunSuccessfulEventV1, eventJSON: pipelineRunSuccessfulEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunFailedEventV1, eventJSON: taskRunFailedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunRunningEventV1, eventJSON: taskRunRunningEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunStartedEventV1, eventJSON: taskRunStartedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunSuccessfulEventV1, eventJSON: taskRunSuccessfulEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunUnknownEventV1, eventJSON: taskRunUnknownEventBytes, want: cloudevents.ResultACK},
	}

	h := &pipelineRunHandler{
		LarkClient: lark.NewClient(larkAppID, larkAppSecret, lark.WithLogReqAtDebug(true), lark.WithEnableTokenCache(true)),
		Tekton: config.Tekton{
			Notifications:    []config.TektonNotification{{Receivers: []string{receiver}}},
			DashboardBaseURL: baseURL,
		},
	}
	for _, tt := range tests {
		t.Run(string(tt.name), func(t *testing.T) {
			e := cloudevents.NewEvent()
			if err := json.Unmarshal(tt.eventJSON, &e); err != nil {
				t.Error(err)
				return
			}

			if got := h.Handle(e); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("pipelineRunHandler.Handle() = %v, want %v", got, tt.want)
			}
		})
	}
}
