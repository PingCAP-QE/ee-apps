package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
)

type stubCloudEventProducer struct {
	events []cloudevents.Event
	topics []string
	result cloudevents.Result
}

func (s *stubCloudEventProducer) HandleCloudEvent(_ context.Context, event cloudevents.Event) cloudevents.Result {
	return s.HandleCloudEventWithTopic(context.Background(), event, "")
}

func (s *stubCloudEventProducer) HandleCloudEventWithTopic(_ context.Context, event cloudevents.Event, topic string) cloudevents.Result {
	s.events = append(s.events, event)
	s.topics = append(s.topics, topic)
	if s.result != nil {
		return s.result
	}
	return cloudevents.ResultACK
}

func TestParseJenkinsPluginCloudEvent(t *testing.T) {
	timestamp := time.Date(2026, 4, 24, 6, 41, 59, 912149395, time.UTC)
	body := buildJenkinsPluginBody(
		"b83dfa7e-8c58-4e73-813d-f362ea3cda54",
		"job/pingcap/job/tidb/job/ghpr_check2/2234/",
		"dev.cdevents.taskrun.finished.0.1.0",
		"application/json",
		timestamp,
		[]byte(`{"label":"»","status":"ERROR"}`),
	)

	event, err := parseJenkinsPluginCloudEvent([]byte(body))
	if err != nil {
		t.Fatalf("parseJenkinsPluginCloudEvent() error = %v", err)
	}

	if got, want := event.ID(), "b83dfa7e-8c58-4e73-813d-f362ea3cda54"; got != want {
		t.Fatalf("event.ID() = %q, want %q", got, want)
	}
	if got, want := event.Type(), "dev.cdevents.taskrun.finished.0.1.0"; got != want {
		t.Fatalf("event.Type() = %q, want %q", got, want)
	}
	if got, want := event.Source(), "job/pingcap/job/tidb/job/ghpr_check2/2234/"; got != want {
		t.Fatalf("event.Source() = %q, want %q", got, want)
	}
	if !event.Time().Equal(timestamp) {
		t.Fatalf("event.Time() = %s, want %s", event.Time(), timestamp)
	}

	var data map[string]string
	if err := event.DataAs(&data); err != nil {
		t.Fatalf("event.DataAs() error = %v", err)
	}
	if got, want := data["label"], "»"; got != want {
		t.Fatalf("data[label] = %q, want %q", got, want)
	}
}

func TestParseStructuredJenkinsCloudEvent(t *testing.T) {
	body := buildStructuredJenkinsBody(
		"0566fa0d-5e16-4234-9ad7-cce56099b54e",
		"job/cdevents-smoke/1/",
		jenkinsPipelineRunFinishedEventType,
		"application/json",
		time.Date(2026, 4, 27, 5, 0, 0, 465924797, time.UTC),
		map[string]any{
			"context": map[string]any{
				"id":        "7dcd202f-0497-4b9f-a0ed-067b6df70428",
				"type":      jenkinsPipelineRunFinishedEventType,
				"source":    "job/cdevents-smoke/1/",
				"version":   "0.1.2",
				"timestamp": "2026-04-27T05:00:00Z",
			},
			"customData": map[string]any{
				"name":        "cdevents-smoke",
				"displayName": "cdevents-smoke",
				"url":         "job/cdevents-smoke/",
				"build": map[string]any{
					"number":   1,
					"queueId":  1,
					"duration": 1040,
					"url":      "job/cdevents-smoke/1/",
				},
			},
			"customDataContentType": "application/json",
			"subject": map[string]any{
				"id":   "1",
				"type": "PIPELINERUN",
				"content": map[string]any{
					"pipelineName": "cdevents-smoke",
					"outcome":      "SUCCESS",
					"errors":       "",
				},
			},
		},
	)

	event, err := parseJenkinsPluginCloudEvent([]byte(body))
	if err != nil {
		t.Fatalf("parseJenkinsPluginCloudEvent() error = %v", err)
	}

	if got, want := event.ID(), "0566fa0d-5e16-4234-9ad7-cce56099b54e"; got != want {
		t.Fatalf("event.ID() = %q, want %q", got, want)
	}
	if got, want := event.Type(), jenkinsPipelineRunFinishedEventType; got != want {
		t.Fatalf("event.Type() = %q, want %q", got, want)
	}
	if got, want := event.SpecVersion(), "0.3"; got != want {
		t.Fatalf("event.SpecVersion() = %q, want %q", got, want)
	}

	var data map[string]any
	if err := event.DataAs(&data); err != nil {
		t.Fatalf("event.DataAs() error = %v", err)
	}
	if got, want := data["customData"].(map[string]any)["name"], "cdevents-smoke"; got != want {
		t.Fatalf("customData.name = %v, want %v", got, want)
	}
}

func TestJenkinsSinkHandlerFuncAcceptedPipelineRunFinished(t *testing.T) {
	gin.SetMode(gin.TestMode)

	producer := &stubCloudEventProducer{}
	router := gin.New()
	router.POST("/jenkins-event", newJenkinsSinkHandlerFunc(producer))

	body := buildJenkinsPluginBody(
		"af50c5d5-a0eb-45e1-9547-a4d6015e8b78",
		"job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1796/",
		jenkinsPipelineRunFinishedEventType,
		"application/json",
		time.Date(2026, 4, 24, 6, 43, 16, 387488262, time.UTC),
		[]byte(`{"status":"ERROR"}`),
	)

	req := httptest.NewRequest(http.MethodPost, "/jenkins-event", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp := httptest.NewRecorder()

	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusOK {
		t.Fatalf("response code = %d, want %d, body = %s", resp.Code, http.StatusOK, resp.Body.String())
	}
	if !strings.Contains(resp.Body.String(), `"status":"accepted"`) {
		t.Fatalf("response body = %s, want accepted json", resp.Body.String())
	}
	if len(producer.events) != 1 {
		t.Fatalf("producer events = %d, want 1", len(producer.events))
	}
	if len(producer.topics) != 1 {
		t.Fatalf("producer topics = %d, want 1", len(producer.topics))
	}
	if got, want := producer.topics[0], jenkinsEventTopic; got != want {
		t.Fatalf("producer topic = %q, want %q", got, want)
	}
	if got, want := producer.events[0].ID(), "af50c5d5-a0eb-45e1-9547-a4d6015e8b78"; got != want {
		t.Fatalf("producer event id = %q, want %q", got, want)
	}
}

func TestJenkinsSinkHandlerFuncAcceptedStructuredPipelineRunFinished(t *testing.T) {
	gin.SetMode(gin.TestMode)

	producer := &stubCloudEventProducer{}
	router := gin.New()
	router.POST("/jenkins-event", newJenkinsSinkHandlerFunc(producer))

	body := buildStructuredJenkinsBody(
		"0566fa0d-5e16-4234-9ad7-cce56099b54e",
		"job/cdevents-smoke/1/",
		jenkinsPipelineRunFinishedEventType,
		"application/json",
		time.Date(2026, 4, 27, 5, 0, 0, 465924797, time.UTC),
		map[string]any{
			"context": map[string]any{
				"id":        "7dcd202f-0497-4b9f-a0ed-067b6df70428",
				"type":      jenkinsPipelineRunFinishedEventType,
				"source":    "job/cdevents-smoke/1/",
				"version":   "0.1.2",
				"timestamp": "2026-04-27T05:00:00Z",
			},
			"customData": map[string]any{
				"name":        "cdevents-smoke",
				"displayName": "cdevents-smoke",
				"url":         "job/cdevents-smoke/",
			},
			"customDataContentType": "application/json",
			"subject": map[string]any{
				"id":   "1",
				"type": "PIPELINERUN",
				"content": map[string]any{
					"pipelineName": "cdevents-smoke",
					"outcome":      "SUCCESS",
					"errors":       "",
				},
			},
		},
	)

	req := httptest.NewRequest(http.MethodPost, "/jenkins-event", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/cloudevents+json")
	resp := httptest.NewRecorder()

	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusOK {
		t.Fatalf("response code = %d, want %d, body = %s", resp.Code, http.StatusOK, resp.Body.String())
	}
	if !strings.Contains(resp.Body.String(), `"status":"accepted"`) {
		t.Fatalf("response body = %s, want accepted json", resp.Body.String())
	}
	if len(producer.events) != 1 {
		t.Fatalf("producer events = %d, want 1", len(producer.events))
	}
	if got, want := producer.events[0].SpecVersion(), "0.3"; got != want {
		t.Fatalf("producer event specversion = %q, want %q", got, want)
	}
}

func TestJenkinsSinkHandlerFuncIgnoredNonFinishedEvent(t *testing.T) {
	gin.SetMode(gin.TestMode)

	producer := &stubCloudEventProducer{}
	router := gin.New()
	router.POST("/jenkins-event", newJenkinsSinkHandlerFunc(producer))

	body := buildJenkinsPluginBody(
		"af50c5d5-a0eb-45e1-9547-a4d6015e8b78",
		"job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1796/",
		"dev.cdevents.taskrun.finished.0.1.0",
		"application/json",
		time.Date(2026, 4, 24, 6, 43, 16, 387488262, time.UTC),
		[]byte(`{"status":"ERROR"}`),
	)

	req := httptest.NewRequest(http.MethodPost, "/jenkins-event", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp := httptest.NewRecorder()

	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusOK {
		t.Fatalf("response code = %d, want %d, body = %s", resp.Code, http.StatusOK, resp.Body.String())
	}
	if !strings.Contains(resp.Body.String(), `"status":"ignored"`) {
		t.Fatalf("response body = %s, want ignored json", resp.Body.String())
	}
	if len(producer.events) != 0 {
		t.Fatalf("producer events = %d, want 0", len(producer.events))
	}
	if len(producer.topics) != 0 {
		t.Fatalf("producer topics = %d, want 0", len(producer.topics))
	}
}

func TestJenkinsSinkHandlerFuncInvalidPayload(t *testing.T) {
	gin.SetMode(gin.TestMode)

	producer := &stubCloudEventProducer{}
	router := gin.New()
	router.POST("/jenkins-event", newJenkinsSinkHandlerFunc(producer))

	req := httptest.NewRequest(http.MethodPost, "/jenkins-event", strings.NewReader(`{"not":"jenkins-plugin-body"}`))
	resp := httptest.NewRecorder()

	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusBadRequest {
		t.Fatalf("response code = %d, want %d", resp.Code, http.StatusBadRequest)
	}
	if len(producer.events) != 0 {
		t.Fatalf("producer events = %d, want 0", len(producer.events))
	}
	if !strings.Contains(resp.Body.String(), `"status":"invalid"`) {
		t.Fatalf("response body = %s, want invalid json", resp.Body.String())
	}
}

func buildJenkinsPluginBody(id, source, eventType, dataContentType string, eventTime time.Time, payload []byte) string {
	signedValues := make([]string, 0, len(payload))
	for _, value := range payload {
		signedValues = append(signedValues, strconv.Itoa(int(int8(value))))
	}

	return fmt.Sprintf(
		"CloudEvent{id='%s', source=%s, type='%s', datacontenttype='%s', time=%s, data=BytesCloudEventData{value=[%s]}, extensions={}}",
		id,
		source,
		eventType,
		dataContentType,
		eventTime.Format(time.RFC3339Nano),
		strings.Join(signedValues, ", "),
	)
}

func buildStructuredJenkinsBody(id, source, eventType, dataContentType string, eventTime time.Time, data map[string]any) string {
	event := map[string]any{
		"specversion":     "0.3",
		"id":              id,
		"source":          source,
		"type":            eventType,
		"datacontenttype": dataContentType,
		"time":            eventTime.Format(time.RFC3339Nano),
		"data":            data,
	}

	payload, err := json.Marshal(event)
	if err != nil {
		panic(err)
	}
	return string(payload)
}
