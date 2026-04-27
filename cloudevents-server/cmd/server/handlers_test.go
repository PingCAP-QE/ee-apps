package main

import (
	"context"
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
