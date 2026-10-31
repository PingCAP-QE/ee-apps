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
	n := NightlyPublish{
		Enabled: &enabled,
		Targets: map[string]NightlyWindow{
			"tiup-staging": {Window: "1h", Anchor: "00:00"},
		},
	}
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
	window := n.WindowFor("tiup-prod")
	if got := window.WindowDuration(); got != 12*time.Hour {
		t.Fatalf("WindowDuration() = %v, want 12h", got)
	}
	if got := window.AnchorOffset(); got != 2*time.Hour {
		t.Fatalf("AnchorOffset() = %v, want 2h", got)
	}
	if got := window.Location(); got != time.UTC {
		t.Fatalf("Location() = %v, want UTC", got)
	}
	if got := window.RetryAfterDuration(); got != 5*time.Minute {
		t.Fatalf("RetryAfterDuration() = %v, want 5m", got)
	}
	if got := window.JitterDuration(); got != 30*time.Minute {
		t.Fatalf("JitterDuration() = %v, want 30m", got)
	}
	if got := n.TickEvery(); got != time.Minute {
		t.Fatalf("TickEvery() = %v, want 1m", got)
	}
	if got := n.BatchSize(); got != 10 {
		t.Fatalf("BatchSize() = %d, want 10", got)
	}
}

func TestNightlyPublishPerTargetWindows(t *testing.T) {
	n := NightlyPublish{
		Default: NightlyWindow{
			Window:     "12h",
			Anchor:     "02:00",
			Timezone:   "UTC",
			RetryAfter: "5m",
			Jitter:     "30m",
			OnFailure:  "alert",
		},
		Targets: map[string]NightlyWindow{
			"tiup-staging": {Window: "1h", Anchor: "00:00"},
			"tiup-prod":    {Window: "12h", Anchor: "02:00"},
		},
	}

	staging := n.WindowFor("tiup-staging")
	if got := staging.WindowDuration(); got != time.Hour {
		t.Fatalf("staging WindowDuration() = %v, want 1h", got)
	}
	if got := staging.AnchorOffset(); got != 0 {
		t.Fatalf("staging AnchorOffset() = %v, want 0", got)
	}
	if got := staging.RetryAfterDuration(); got != 5*time.Minute {
		t.Fatalf("staging RetryAfterDuration() = %v, want the default 5m", got)
	}
	if got := staging.JitterDuration(); got != 15*time.Minute {
		t.Fatalf("staging JitterDuration() = %v, want window/4 = 15m", got)
	}

	prod := n.WindowFor("tiup-prod")
	if got := prod.WindowDuration(); got != 12*time.Hour {
		t.Fatalf("prod WindowDuration() = %v, want 12h", got)
	}
	if got := prod.AnchorOffset(); got != 2*time.Hour {
		t.Fatalf("prod AnchorOffset() = %v, want 2h", got)
	}
	if got := prod.JitterDuration(); got != 30*time.Minute {
		t.Fatalf("prod JitterDuration() = %v, want 30m", got)
	}

	inherited := n.WindowFor("other")
	if got := inherited.WindowDuration(); got != 12*time.Hour {
		t.Fatalf("inherited WindowDuration() = %v, want the default 12h", got)
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
		Enabled: &enabled,
		Default: NightlyWindow{
			Window:     "12h",
			Anchor:     "02:00",
			Timezone:   "Asia/Shanghai",
			RetryAfter: "5m",
			Jitter:     "30m",
			OnFailure:  "alert",
		},
		Targets: map[string]NightlyWindow{
			"tiup-staging": {Window: "1h", Anchor: "00:00"},
		},
	}
	if err := valid.Validate(); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}

	rollback := valid
	rollback.Default.OnFailure = "rollback"
	if err := rollback.Validate(); err == nil {
		t.Fatal("Validate() = nil for on_failure=rollback, want the unsupported error")
	}

	badWindow := valid
	badWindow.Default.Window = "sometimes"
	if err := badWindow.Validate(); err == nil {
		t.Fatal("Validate() = nil for a malformed window")
	}

	badZone := valid
	badZone.Default.Timezone = "Mars/Olympus"
	if err := badZone.Validate(); err == nil {
		t.Fatal("Validate() = nil for an unknown timezone")
	}

	hugeJitter := valid
	hugeJitter.Default.Jitter = "24h"
	if err := hugeJitter.Validate(); err == nil {
		t.Fatal("Validate() = nil when jitter is not shorter than the window")
	}

	badTarget := valid
	badTarget.Targets = map[string]NightlyWindow{
		"tiup-staging": {Window: "sometimes"},
	}
	if err := badTarget.Validate(); err == nil {
		t.Fatal("Validate() = nil for a malformed target window")
	}

	// A disabled section never blocks startup, even with nonsense values.
	disabled := NightlyPublish{
		Default: NightlyWindow{Window: "sometimes", OnFailure: "rollback"},
	}
	if err := disabled.Validate(); err != nil {
		t.Fatalf("disabled Validate() error = %v, want nil", err)
	}
}
