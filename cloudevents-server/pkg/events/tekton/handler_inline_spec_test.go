package tekton

import (
	"encoding/json"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	corev1 "k8s.io/api/core/v1"
	"knative.dev/pkg/apis"
	duckv1 "knative.dev/pkg/apis/duck/v1"
)

const testEventSource = "/apis/tekton.dev/v1/namespaces/ee-cd/taskruns/inline-run"

func failedConditions(message string) duckv1.Conditions {
	return duckv1.Conditions{{
		Type:    apis.ConditionSucceeded,
		Status:  corev1.ConditionFalse,
		Reason:  "Failed",
		Message: message,
	}}
}

func newTestEvent(t *testing.T, eventType string, data tektoncloudevent.TektonCloudEventData) cloudevents.Event {
	t.Helper()

	event := cloudevents.NewEvent()
	event.SetID("test-event")
	event.SetType(eventType)
	event.SetSource(testEventSource)
	if err := event.SetData(cloudevents.ApplicationJSON, data); err != nil {
		t.Fatal(err)
	}

	return event
}

// A failed run declared with an inline spec carries no TaskRef/PipelineRef. The
// notification must still be built, and must not invent a rerun command that
// dereferences the missing ref.
func TestFailedTaskRunWithInlineSpecKeepsNotification(t *testing.T) {
	run := &v1beta1.TaskRun{}
	run.Name = "inline-taskrun"
	run.Namespace = "ee-cd"
	run.Spec.TaskSpec = &v1beta1.TaskSpec{}
	run.Status.Conditions = failedConditions("boom")

	infos, err := extractLarkInfosFromEvent(
		newTestEvent(t, string(tektoncloudevent.TaskRunFailedEventV1), tektoncloudevent.TektonCloudEventData{TaskRun: run}),
		"https://dashboard.example/tekton",
	)
	if err != nil {
		t.Fatal(err)
	}
	if infos == nil {
		t.Fatal("no notification built for an inline taskSpec run")
	}
	if infos.FailedMessage != "boom" {
		t.Errorf("FailedMessage = %q, want %q", infos.FailedMessage, "boom")
	}
	if infos.RerunURL != "" {
		t.Errorf("RerunURL = %q, want empty for an inline taskSpec run", infos.RerunURL)
	}
}

func TestFailedPipelineRunWithInlineSpecKeepsNotification(t *testing.T) {
	run := &v1beta1.PipelineRun{}
	run.Name = "inline-pipelinerun"
	run.Namespace = "ee-cd"
	run.Spec.PipelineSpec = &v1beta1.PipelineSpec{}
	run.Status.Conditions = failedConditions("boom")

	infos, err := extractLarkInfosFromEvent(
		newTestEvent(t, string(tektoncloudevent.PipelineRunFailedEventV1), tektoncloudevent.TektonCloudEventData{PipelineRun: run}),
		"https://dashboard.example/tekton",
	)
	if err != nil {
		t.Fatal(err)
	}
	if infos == nil {
		t.Fatal("no notification built for an inline pipelineSpec run")
	}
	if infos.FailedMessage != "boom" {
		t.Errorf("FailedMessage = %q, want %q", infos.FailedMessage, "boom")
	}
	if infos.RerunURL != "" {
		t.Errorf("RerunURL = %q, want empty for an inline pipelineSpec run", infos.RerunURL)
	}
}

func TestFailedRunsWithRefsKeepRerunCommand(t *testing.T) {
	for _, tc := range []struct {
		name  string
		event []byte
		want  string
	}{
		{
			name:  "PipelineRun",
			event: pipelineRunFailedEventBytes,
			want:  "tkn -n ee-cd pipeline start pingcap-build-package --use-pipelinerun build-package-tikv-tikv-linux-9bn55",
		},
		{
			name:  "standalone TaskRun",
			event: standaloneTaskRunFailedEventBytes,
			want:  "tkn -n ee-cd task start pingcap-build-binaries --use-taskrun task-run-standalone-12345",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			event := cloudevents.NewEvent()
			if err := json.Unmarshal(tc.event, &event); err != nil {
				t.Fatal(err)
			}

			infos, err := extractLarkInfosFromEvent(event, "https://dashboard.example/tekton")
			if err != nil {
				t.Fatal(err)
			}
			if infos == nil || infos.RerunURL != tc.want {
				t.Fatalf("RerunURL = %v, want %q", infos, tc.want)
			}
		})
	}
}
