package config

import (
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

func TestWorkerApplyDefaults(t *testing.T) {
	var w Worker
	w.ApplyDefaults()

	if w.ID == "" {
		t.Fatal("ID should default to a non-empty value")
	}
	if len(w.Targets) != 1 || w.Targets[0] != "*" {
		t.Fatalf("Targets = %v, want [*]", w.Targets)
	}
	if w.Concurrency != 1 {
		t.Fatalf("Concurrency = %d, want 1", w.Concurrency)
	}
	if got := w.PollEvery(); got != 10*time.Second {
		t.Fatalf("PollEvery() = %v, want 10s", got)
	}
	if got := w.LeaseDuration(); got != 90*time.Second {
		t.Fatalf("LeaseDuration() = %v, want 90s", got)
	}
	if got := w.HeartbeatDuration(); got != 30*time.Second {
		t.Fatalf("HeartbeatDuration() = %v, want 30s", got)
	}
	if got := w.HardTimeoutDuration(); got != 0 {
		t.Fatalf("HardTimeoutDuration() = %v, want 0 (disabled)", got)
	}
	if w.MaxAttempts != 3 {
		t.Fatalf("MaxAttempts = %d, want 3", w.MaxAttempts)
	}
	if len(w.Kinds) != 1 || w.Kinds[0] != "image_copy" {
		t.Fatalf("Kinds = %v, want [image_copy]", w.Kinds)
	}
}

func TestWorkerApplyDefaultsKeepsExplicitValues(t *testing.T) {
	w := Worker{
		ID:           "tke-bj-image-0",
		Targets:      []string{"gcr.io"},
		Concurrency:  4,
		PollInterval: "2s",
		Lease:        "120s",
		Heartbeat:    "40s",
		HardTimeout:  "50s",
		MaxAttempts:  5,
		Kinds:        []string{"image_copy"},
	}
	w.ApplyDefaults()

	if w.ID != "tke-bj-image-0" || w.Concurrency != 4 || w.MaxAttempts != 5 {
		t.Fatalf("explicit values were overwritten: %+v", w)
	}
	if got := w.PollEvery(); got != 2*time.Second {
		t.Fatalf("PollEvery() = %v, want 2s", got)
	}
	if got := w.HardTimeoutDuration(); got != 50*time.Second {
		t.Fatalf("HardTimeoutDuration() = %v, want 50s", got)
	}
}

func TestWorkerValidateRejectsInvalidLease(t *testing.T) {
	w := Worker{Lease: "30s", Heartbeat: "30s"}
	if err := w.Validate(nil); err == nil || !strings.Contains(err.Error(), "lease") {
		t.Fatalf("Validate() error = %v, want lease error", err)
	}
}

func TestWorkerValidateRejectsUnknownTarget(t *testing.T) {
	w := Worker{Targets: []string{"gcr.io", "docker.io"}}
	err := w.Validate([]string{"docker.io"})
	if err == nil || !strings.Contains(err.Error(), "gcr.io") {
		t.Fatalf("Validate() error = %v, want unknown target error", err)
	}
}

func TestWorkerValidateAcceptsWildcard(t *testing.T) {
	w := Worker{Targets: []string{"*"}}
	if err := w.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
}

func TestWorkerValidateAllowsConcreteTargetsWithoutCatalogue(t *testing.T) {
	// Image workers have no catalogue here; delivery.yaml is authoritative.
	w := Worker{Targets: []string{"docker.io", "gcr.io"}}
	if err := w.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
}

func TestWorkerValidateUsesConfiguredCatalogue(t *testing.T) {
	w := Worker{Targets: []string{"tiup-staging"}}
	if err := w.Validate([]string{"tiup-staging", "tiup-prod"}); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
	if err := w.Validate([]string{"tiup-prod"}); err == nil {
		t.Fatal("Validate() = nil, want an unknown target error outside the catalogue")
	}
}

func TestWorkerValidateRejectsMalformedDurations(t *testing.T) {
	for name, w := range map[string]Worker{
		"poll_interval": {PollInterval: "soon"},
		"lease":         {Lease: "soon"},
		"heartbeat":     {Heartbeat: "soon"},
	} {
		t.Run(name, func(t *testing.T) {
			if err := w.Validate(nil); err == nil {
				t.Fatalf("Validate() = nil, want error for %s", name)
			}
		})
	}
}

func TestWorkerValidateHardTimeout(t *testing.T) {
	valid := Worker{Lease: "90s", Heartbeat: "30s", HardTimeout: "60s"}
	if err := valid.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
	tooLong := Worker{Lease: "90s", Heartbeat: "30s", HardTimeout: "90s"}
	if err := tooLong.Validate(nil); err == nil {
		t.Fatal("Validate() = nil, want hard_timeout error")
	}
	malformed := Worker{Lease: "90s", Heartbeat: "30s", HardTimeout: "soon"}
	if err := malformed.Validate(nil); err == nil {
		t.Fatal("Validate() = nil, want duration error")
	}
}

// The observability knobs must default to the values that used to be hard-coded
// constants, so an untouched config keeps the current alerting behaviour.
func TestWorkerObservabilityDefaults(t *testing.T) {
	var w Worker
	w.ApplyDefaults()

	for _, tc := range []struct {
		name string
		got  time.Duration
		want time.Duration
	}{
		{"MetricsEvery", w.MetricsEvery(), 15 * time.Second},
		{"ReapEvery", w.ReapEvery(), 30 * time.Second},
		{"BackoffBaseDuration", w.BackoffBaseDuration(), 10 * time.Second},
		{"BackoffMaxDuration", w.BackoffMaxDuration(), 5 * time.Minute},
		{"AlertBacklogAfter", w.AlertBacklogAfter(), 10 * time.Minute},
		{"AlertOldestPendingAfter", w.AlertOldestPendingAfter(), 30 * time.Minute},
		{"AlertLeaseReclaimWindow", w.AlertLeaseReclaimWindow(), 5 * time.Minute},
		{"AlertDedupe", w.AlertDedupe(), time.Hour},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %v, want %v", tc.name, tc.got, tc.want)
		}
	}
	if !w.AlertsEnabled() {
		t.Error("AlertsEnabled() = false, want true by default")
	}
}

func TestWorkerObservabilityExplicitValues(t *testing.T) {
	enabled := false
	w := Worker{
		MetricsInterval: "5s",
		ReapInterval:    "7s",
		BackoffBase:     "1s",
		BackoffMax:      "2m",
		Alerts: WorkerAlerts{
			Enabled:            &enabled,
			BacklogAfter:       "30s",
			OldestPendingAfter: "1m",
			LeaseReclaimWindow: "2m",
			Dedupe:             "3m",
		},
	}
	w.ApplyDefaults()

	for _, tc := range []struct {
		name string
		got  time.Duration
		want time.Duration
	}{
		{"MetricsEvery", w.MetricsEvery(), 5 * time.Second},
		{"ReapEvery", w.ReapEvery(), 7 * time.Second},
		{"BackoffBaseDuration", w.BackoffBaseDuration(), time.Second},
		{"BackoffMaxDuration", w.BackoffMaxDuration(), 2 * time.Minute},
		{"AlertBacklogAfter", w.AlertBacklogAfter(), 30 * time.Second},
		{"AlertOldestPendingAfter", w.AlertOldestPendingAfter(), time.Minute},
		{"AlertLeaseReclaimWindow", w.AlertLeaseReclaimWindow(), 2 * time.Minute},
		{"AlertDedupe", w.AlertDedupe(), 3 * time.Minute},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %v, want %v", tc.name, tc.got, tc.want)
		}
	}
	if w.AlertsEnabled() {
		t.Error("AlertsEnabled() = true, want false when explicitly disabled")
	}
}

// A missing alerts.enabled key must stay enabled, while an explicit false must
// survive YAML decoding. That is why the field is a pointer.
func TestWorkerAlertsEnabledYAML(t *testing.T) {
	for name, tc := range map[string]struct {
		raw  string
		want bool
	}{
		"absent":  {raw: "worker: {}\n", want: true},
		"omitted": {raw: "worker:\n  alerts:\n    dedupe: \"2h\"\n", want: true},
		"false":   {raw: "worker:\n  alerts:\n    enabled: false\n", want: false},
		"true":    {raw: "worker:\n  alerts:\n    enabled: true\n", want: true},
	} {
		t.Run(name, func(t *testing.T) {
			var svc Service
			if err := yaml.Unmarshal([]byte(tc.raw), &svc); err != nil {
				t.Fatalf("yaml.Unmarshal() error = %v", err)
			}
			svc.Worker.ApplyDefaults()
			if got := svc.Worker.AlertsEnabled(); got != tc.want {
				t.Fatalf("AlertsEnabled() = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestWorkerValidateRejectsInvalidObservabilityDurations(t *testing.T) {
	cases := map[string]Worker{
		"metrics_interval malformed":  {MetricsInterval: "soon"},
		"metrics_interval zero":       {MetricsInterval: "0s"},
		"metrics_interval negative":   {MetricsInterval: "-5s"},
		"reap_interval malformed":     {ReapInterval: "soon"},
		"reap_interval zero":          {ReapInterval: "0s"},
		"backoff_base negative":       {BackoffBase: "-1s"},
		"backoff_max zero":            {BackoffMax: "0s"},
		"alerts.backlog_after":        {Alerts: WorkerAlerts{BacklogAfter: "soon"}},
		"alerts.oldest_pending":       {Alerts: WorkerAlerts{OldestPendingAfter: "-1m"}},
		"alerts.lease_reclaim_window": {Alerts: WorkerAlerts{LeaseReclaimWindow: "0s"}},
		"alerts.dedupe":               {Alerts: WorkerAlerts{Dedupe: "soon"}},
	}
	for name, w := range cases {
		t.Run(name, func(t *testing.T) {
			if err := w.Validate(nil); err == nil {
				t.Fatalf("Validate() = nil, want error for %s", name)
			}
		})
	}
}

func TestWorkerValidateAcceptsObservabilityDurations(t *testing.T) {
	enabled := false
	w := Worker{
		MetricsInterval: "2s",
		ReapInterval:    "5s",
		BackoffBase:     "1s",
		BackoffMax:      "30s",
		Alerts: WorkerAlerts{
			Enabled:            &enabled,
			BacklogAfter:       "30s",
			OldestPendingAfter: "1m",
			LeaseReclaimWindow: "2m",
			Dedupe:             "5m",
		},
	}
	if err := w.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
}
