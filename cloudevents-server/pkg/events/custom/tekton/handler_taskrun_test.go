package tekton

import (
	"encoding/json"
	"reflect"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	_ "embed"
)

// test events.
var (
	//go:embed testdata/event-taskrun.failed.json
	taskRunFailedEventBytes []byte
	//go:embed testdata/event-taskrun.running.json
	taskRunRunningEventBytes []byte
	//go:embed testdata/event-taskrun.started.json
	taskRunStartedEventBytes []byte
	//go:embed testdata/event-taskrun.successful.json
	taskRunSuccessfulEventBytes []byte
	//go:embed testdata/event-taskrun.unknown.json
	taskRunUnknownEventBytes []byte
)

func Test_taskRunHandler_Handle(t *testing.T) {
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
		{name: tektoncloudevent.TaskRunFailedEventV1, eventJSON: taskRunFailedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunRunningEventV1, eventJSON: taskRunRunningEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunStartedEventV1, eventJSON: taskRunStartedEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunSuccessfulEventV1, eventJSON: taskRunSuccessfulEventBytes, want: cloudevents.ResultACK},
		{name: tektoncloudevent.TaskRunUnknownEventV1, eventJSON: taskRunUnknownEventBytes, want: cloudevents.ResultACK},
	}

	h := &taskRunHandler{
		LarkClient:       lark.NewClient(larkAppID, larkAppSecret, lark.WithLogReqAtDebug(true), lark.WithEnableTokenCache(true)),
		Receivers:        []string{receiver},
		RunDetailBaseURL: baseURL,
	}
	for _, tt := range tests {
		t.Run(string(tt.name), func(t *testing.T) {
			e := cloudevents.NewEvent()
			if err := json.Unmarshal(tt.eventJSON, &e); err != nil {
				t.Error(err)
				return
			}

			if got := h.Handle(e); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("taskRunHandler.Handle() = %v, want %v", got, tt.want)
			}
		})
	}
}
