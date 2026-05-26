package handler

import (
	"context"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
)

func TestResolveTopic(t *testing.T) {
	producer := &EventProducer{
		unknowEventTopic: "cloudevents-default-channel",
		topicMapping: map[string]string{
			"dev.cdevents.pipelinerun.finished.0.1.0": "jenkins-event",
		},
	}

	tests := []struct {
		name       string
		eventType  string
		wantTopic  string
		wantIgnore bool
	}{
		{
			name:       "mapped Jenkins pipeline run finished event",
			eventType:  "dev.cdevents.pipelinerun.finished.0.1.0",
			wantTopic:  "jenkins-event",
			wantIgnore: false,
		},
		{
			name:       "unmapped Jenkins event is ignored",
			eventType:  "dev.cdevents.taskrun.finished.0.1.0",
			wantTopic:  "",
			wantIgnore: true,
		},
		{
			name:       "non Jenkins event falls back to default topic",
			eventType:  "dev.tekton.event.taskrun.failed.v1",
			wantTopic:  "cloudevents-default-channel",
			wantIgnore: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotTopic, gotIgnore := producer.resolveTopic(tt.eventType)
			if gotTopic != tt.wantTopic {
				t.Fatalf("resolveTopic() topic = %q, want %q", gotTopic, tt.wantTopic)
			}
			if gotIgnore != tt.wantIgnore {
				t.Fatalf("resolveTopic() ignore = %v, want %v", gotIgnore, tt.wantIgnore)
			}
		})
	}
}

func TestHandleCloudEventIgnoresUnsupportedJenkinsCDEvent(t *testing.T) {
	producer := &EventProducer{
		unknowEventTopic: "cloudevents-default-channel",
		topicMapping: map[string]string{
			"dev.cdevents.pipelinerun.finished.0.1.0": "jenkins-event",
		},
	}

	event := cloudevents.NewEvent()
	event.SetID("test-id")
	event.SetSource("job/example/1/")
	event.SetType("dev.cdevents.taskrun.finished.0.1.0")

	result := producer.HandleCloudEvent(context.Background(), event)
	if !cloudevents.IsACK(result) {
		t.Fatalf("HandleCloudEvent() result = %v, want ACK", result)
	}
}
