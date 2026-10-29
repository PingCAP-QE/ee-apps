package tekton

import (
	"encoding/json"
	"reflect"
	"sort"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"

	_ "embed"
)

//go:embed testdata/event-pipelinerun.devbuild.failed.json
var pipelineRunDevbuildFailedEventBytes []byte

func newPipelineRunWithContext(t *testing.T, rawContext string) *v1beta1.PipelineRun {
	t.Helper()

	run := new(v1beta1.PipelineRun)
	if rawContext != "" {
		run.Annotations = map[string]string{eventContextAnnotationKey: rawContext}
	}

	return run
}

func newEventContextJSON(t *testing.T, source, subject, user string) string {
	t.Helper()

	bs, err := json.Marshal(eventContext{Source: source, Subject: subject, User: user})
	if err != nil {
		t.Fatal(err)
	}

	return string(bs)
}

func newPipelineRunEvent(typ, subject string) cloudevents.Event {
	event := cloudevents.NewEvent()
	event.SetType(typ)
	event.SetSubject(subject)

	return event
}

func assertReceivers(t *testing.T, got, want []string) {
	t.Helper()

	gotCopy := append([]string(nil), got...)
	wantCopy := append([]string(nil), want...)
	sort.Strings(gotCopy)
	sort.Strings(wantCopy)

	if len(gotCopy) == 0 && len(wantCopy) == 0 {
		return
	}
	if !reflect.DeepEqual(gotCopy, wantCopy) {
		t.Errorf("receivers = %v, want %v", got, want)
	}
}

func TestIsDevbuildSource(t *testing.T) {
	tests := []struct {
		name   string
		source string
		want   bool
	}{
		{name: "tibuild v1 source", source: "tibuild.pingcap.net/api/devbuilds/2820036", want: true},
		{name: "tibuild v2 source", source: "tibuild.pingcap.net/api/v2/devbuilds/2820036", want: true},
		{name: "with scheme prefix", source: "https://tibuild.pingcap.net/api/v2/devbuilds/2820036", want: false},
		{name: "near miss devbuilds-other", source: "tibuild.pingcap.net/api/devbuilds-other/2820036", want: false},
		{name: "unrelated tibuild source", source: "tibuild.pingcap.net/api/notify/something", want: false},
		{name: "empty source", source: "", want: false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isDevbuildSource(tt.source); got != tt.want {
				t.Errorf("isDevbuildSource(%q) = %v, want %v", tt.source, got, tt.want)
			}
		})
	}
}

func TestResolveReceivers(t *testing.T) {
	failedType := string(tektoncloudevent.PipelineRunFailedEventV1)
	defaultConfigs := []config.TektonNotification{
		{EventType: "*", Receivers: []string{"oc_builds"}},
		{EventType: failedType, EventSubjectReg: "^bp-", Receivers: []string{"oc_bp_failed"}},
	}
	noMatchConfigs := []config.TektonNotification{
		{EventType: "*", EventSubjectReg: "^no-such-subject-", Receivers: []string{"oc_never"}},
	}

	tests := []struct {
		name        string
		annotations string
		eventType   string
		subject     string
		configs     []config.TektonNotification
		want        []string
	}{
		{
			name:        "non devbuild with trigger user",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/notify/1", "bp-tikv", "ou_trigger"),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"ou_trigger"},
		},
		{
			name:        "non devbuild without trigger user",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/notify/1", "bp-tikv", ""),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"oc_bp_failed", "oc_builds"},
		},
		{
			name:        "devbuild v1 skips trigger user",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/devbuilds/2820036", "2820036", "ou_devbuild_creator"),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"oc_bp_failed", "oc_builds"},
		},
		{
			name:        "devbuild v2 skips trigger user",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/v2/devbuilds/2820036", "2820036", "ou_devbuild_creator"),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"oc_bp_failed", "oc_builds"},
		},
		{
			name:        "devbuild with no matching config",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/v2/devbuilds/2820036", "2820036", "ou_devbuild_creator"),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     noMatchConfigs,
			want:        nil,
		},
		{
			name:        "missing ce-context annotation",
			annotations: "",
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"oc_bp_failed", "oc_builds"},
		},
		{
			name:        "invalid ce-context json",
			annotations: "{not-a-json",
			eventType:   failedType,
			subject:     "bp-tikv",
			configs:     defaultConfigs,
			want:        []string{"oc_bp_failed", "oc_builds"},
		},
		{
			name:        "devbuild still honors subject regex hit",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/v2/devbuilds/2820036", "2820036", "ou_devbuild_creator"),
			eventType:   failedType,
			subject:     "bp-tikv",
			configs: []config.TektonNotification{
				{EventType: failedType, EventSubjectReg: "^bp-", Receivers: []string{"oc_bp_failed"}},
			},
			want: []string{"oc_bp_failed"},
		},
		{
			name:        "devbuild still honors subject regex miss",
			annotations: newEventContextJSON(t, "tibuild.pingcap.net/api/v2/devbuilds/2820036", "2820036", "ou_devbuild_creator"),
			eventType:   failedType,
			subject:     "build-package-tikv",
			configs: []config.TektonNotification{
				{EventType: failedType, EventSubjectReg: "^bp-", Receivers: []string{"oc_bp_failed"}},
			},
			want: nil,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			run := newPipelineRunWithContext(t, tt.annotations)
			event := newPipelineRunEvent(tt.eventType, tt.subject)

			got := resolveReceivers(run, event, tt.configs)
			assertReceivers(t, got, tt.want)
		})
	}
}

func TestPipelineRunHandlerSkipsTriggerUserForDevbuild(t *testing.T) {
	event := cloudevents.NewEvent()
	if err := json.Unmarshal(pipelineRunDevbuildFailedEventBytes, &event); err != nil {
		t.Fatal(err)
	}

	var gotReceivers []string
	h := &pipelineRunHandler{
		Tekton: config.Tekton{
			Notifications:    []config.TektonNotification{{Receivers: []string{"oc_builds"}}},
			DashboardBaseURL: "http://127.0.0.1:1",
		},
		notify: func(receivers []string, infos *cardMessageInfos) cloudevents.Result {
			gotReceivers = receivers
			return cloudevents.ResultACK
		},
	}

	if got := h.Handle(event); !cloudevents.IsACK(got) {
		t.Fatalf("pipelineRunHandler.Handle() = %v, want ACK", got)
	}

	assertReceivers(t, gotReceivers, []string{"oc_builds"})
}
