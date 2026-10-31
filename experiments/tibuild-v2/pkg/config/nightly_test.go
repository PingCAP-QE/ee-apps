package config

import (
	"testing"
	"time"
)

func TestNightlyPublishDefaultsAndTargets(t *testing.T) {
	var disabled NightlyPublish
	if disabled.EnabledValue() {
		t.Fatal("an omitted nightly section must keep the legacy behaviour")
	}
	if got := disabled.DesiredTargets([]string{"tiup-prod"}); got != nil {
		t.Fatalf("DesiredTargets() = %v, want nil while disabled", got)
	}

	enabled := true
	n := NightlyPublish{Enabled: &enabled, Targets: []string{"tiup-staging"}}
	if !n.EnabledValue() {
		t.Fatal("EnabledValue() = false, want true")
	}
	if got := n.DesiredTargets([]string{"tiup-prod"}); len(got) != 1 || got[0] != "tiup-staging" {
		t.Fatalf("DesiredTargets() = %v, want the explicit target list", got)
	}
	if !n.WantsTarget("tiup-staging", []string{"tiup-prod"}) {
		t.Fatal("WantsTarget() = false for the configured target")
	}
	if n.WantsTarget("tiup-prod", []string{"tiup-prod"}) {
		t.Fatal("WantsTarget() = true for a target that is not configured")
	}

	inherit := NightlyPublish{Enabled: &enabled}
	if got := inherit.DesiredTargets([]string{"tiup-prod", "tiup-staging"}); len(got) != 2 {
		t.Fatalf("DesiredTargets() = %v, want the publish targets", got)
	}
	if !inherit.WantsTarget("anything", []string{"*"}) {
		t.Fatal("WantsTarget() = false for the wildcard target")
	}
}

func TestNightlyPublishDefaults(t *testing.T) {
	n := NightlyPublish{}
	if got := n.WindowDuration(); got != 12*time.Hour {
		t.Fatalf("WindowDuration() = %v, want 12h", got)
	}
	if got := n.AnchorOffset(); got != 2*time.Hour {
		t.Fatalf("AnchorOffset() = %v, want 2h", got)
	}
	if got := n.Location(); got != time.UTC {
		t.Fatalf("Location() = %v, want UTC", got)
	}
	if got := n.RetryAfterDuration(); got != 5*time.Minute {
		t.Fatalf("RetryAfterDuration() = %v, want 5m", got)
	}
	if got := n.JitterDuration(); got != 30*time.Minute {
		t.Fatalf("JitterDuration() = %v, want 30m", got)
	}
	if got := n.TickEvery(); got != time.Minute {
		t.Fatalf("TickEvery() = %v, want 1m", got)
	}
	if got := n.BatchSize(); got != 10 {
		t.Fatalf("BatchSize() = %d, want 10", got)
	}
}

func TestParseNightlyAnchor(t *testing.T) {
	cases := map[string]time.Duration{
		"02:00":    2 * time.Hour,
		"2:30":     2*time.Hour + 30*time.Minute,
		"23:59:30": 23*time.Hour + 59*time.Minute + 30*time.Second,
		"":         2 * time.Hour,
	}
	for value, want := range cases {
		got, err := ParseNightlyAnchor(value)
		if err != nil {
			t.Fatalf("ParseNightlyAnchor(%q) error = %v", value, err)
		}
		if got != want {
			t.Fatalf("ParseNightlyAnchor(%q) = %v, want %v", value, got, want)
		}
	}
	if _, err := ParseNightlyAnchor("25:00"); err == nil {
		t.Fatal("ParseNightlyAnchor(25:00) = nil error, want an error")
	}
}

func TestNightlyPublishValidate(t *testing.T) {
	enabled := true
	valid := NightlyPublish{
		Enabled:      &enabled,
		Targets:      []string{"tiup-staging"},
		Window:       "12h",
		Anchor:       "02:00",
		Timezone:     "Asia/Shanghai",
		RetryAfter:   "5m",
		Jitter:       "30m",
		TickInterval: "1m",
		OnFailure:    "alert",
	}
	if err := valid.Validate(); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}

	rollback := valid
	rollback.OnFailure = "rollback"
	if err := rollback.Validate(); err == nil {
		t.Fatal("Validate() = nil for on_failure=rollback, want the unsupported error")
	}

	badWindow := valid
	badWindow.Window = "sometimes"
	if err := badWindow.Validate(); err == nil {
		t.Fatal("Validate() = nil for a malformed window")
	}

	badZone := valid
	badZone.Timezone = "Mars/Olympus"
	if err := badZone.Validate(); err == nil {
		t.Fatal("Validate() = nil for an unknown timezone")
	}

	hugeJitter := valid
	hugeJitter.Jitter = "24h"
	if err := hugeJitter.Validate(); err == nil {
		t.Fatal("Validate() = nil when jitter is not shorter than the window")
	}

	// A disabled section never blocks startup, even with nonsense values.
	disabled := NightlyPublish{Window: "sometimes", OnFailure: "rollback"}
	if err := disabled.Validate(); err != nil {
		t.Fatalf("disabled Validate() error = %v, want nil", err)
	}
}
