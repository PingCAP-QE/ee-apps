package config

import (
	"strings"
	"testing"
	"time"
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
