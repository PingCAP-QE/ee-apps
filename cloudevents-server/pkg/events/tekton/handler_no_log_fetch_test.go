package tekton

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

const fetchedStepLogMarker = "STEP-LOG-MUST-NOT-REACH-THE-CARD"

// Tekton failure notifications must not read pod logs. The log target comes
// from an unauthenticated CloudEvent, so fetching it turns the notifier into an
// arbitrary pod-log reader for anyone who can post an event. The card keeps the
// failed step names and Terminated reasons and links to the Dashboard instead.
func TestTektonFailureNotificationsDoNotFetchStepLogs(t *testing.T) {
	fixtures := []struct {
		name    string
		event   []byte
		runPath string
	}{
		{"PipelineRun", pipelineRunFailedEventBytes, "/pipelineruns/"},
		{"standalone TaskRun", standaloneTaskRunFailedEventBytes, "/taskruns/"},
	}

	for _, fixture := range fixtures {
		t.Run(fixture.name, func(t *testing.T) {
			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				requests.Add(1)
				fmt.Fprint(w, fetchedStepLogMarker)
			}))
			defer server.Close()

			var infos *cardMessageInfos
			notify := func(_ []string, got *cardMessageInfos) cloudevents.Result {
				infos = got
				return cloudevents.ResultACK
			}
			tektonConfig := config.Tekton{
				DashboardBaseURL: server.URL + "/tekton",
				Notifications:    []config.TektonNotification{{Receivers: []string{"oc_builds"}}},
			}

			event := cloudevents.NewEvent()
			if err := json.Unmarshal(fixture.event, &event); err != nil {
				t.Fatal(err)
			}

			var handler interface {
				Handle(cloudevents.Event) cloudevents.Result
			}
			switch tektoncloudevent.TektonEventType(event.Type()) {
			case tektoncloudevent.PipelineRunFailedEventV1:
				handler = &pipelineRunHandler{Tekton: tektonConfig, notify: notify}
			case tektoncloudevent.TaskRunFailedEventV1:
				handler = &taskRunHandler{Tekton: tektonConfig, notify: notify}
			default:
				t.Fatalf("unexpected event type %q", event.Type())
			}

			if got := handler.Handle(event); !cloudevents.IsACK(got) {
				t.Fatalf("Handle() = %v, want ACK", got)
			}
			if got := requests.Load(); got != 0 {
				t.Errorf("failure notification issued %d Tekton log request(s), want 0", got)
			}
			if infos == nil {
				t.Fatal("notification was not composed")
			}

			card, err := newLarkCardWithGoTemplate(infos)
			if err != nil {
				t.Fatalf("render card: %v", err)
			}
			if strings.Contains(card, fetchedStepLogMarker) {
				t.Error("card embedded text fetched from the Tekton log endpoint")
			}
			if strings.Contains(card, "```log") {
				t.Error("card still renders a log code block")
			}
			if !strings.Contains(card, "1. build: Error") {
				t.Errorf("card lost the failed step status:\n%s", card)
			}
			if want := server.URL + "/tekton/#/namespaces/ee-cd" + fixture.runPath; !strings.Contains(card, want) {
				t.Errorf("card lost the dashboard view link %q:\n%s", want, card)
			}
		})
	}
}
