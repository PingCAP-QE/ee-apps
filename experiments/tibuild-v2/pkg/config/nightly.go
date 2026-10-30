package config

import (
	"fmt"
	"strings"
	"time"
)

// Nightly defaults. The window is anchored to 02:00 UTC: the anchor makes the
// publish time predictable and independent of the first push, which the legacy
// rate limiter got wrong.
const (
	DefaultNightlyWindow     = 12 * time.Hour
	DefaultNightlyAnchor     = "02:00"
	DefaultNightlyTimezone   = "UTC"
	DefaultNightlyRetryAfter = 5 * time.Minute
	DefaultNightlyJitter     = 30 * time.Minute
	DefaultNightlyTick       = time.Minute
	DefaultNightlyBatch      = 10
)

// NightlyPublish configures nightly convergence. When disabled (the default)
// nightly requests keep the pre-track behaviour: every request publishes
// immediately, serialized by the lane. Enabling it moves the listed targets to
// the desired-state reconciler: a push only upserts the newest artifact and a
// window-aligned tick publishes it.
//
// Targets is the staging-first rollout knob: with `targets: [tiup-staging]`
// only staging converges while prod keeps publishing on every request.
type NightlyPublish struct {
	// Enabled switches the listed targets to desired-state reconciliation. It
	// is a pointer so an omitted key means "keep the legacy behaviour" while
	// an explicit `enabled: false` is preserved.
	Enabled *bool `yaml:"enabled,omitempty" json:"enabled,omitempty"`
	// Targets lists the publish targets whose nightly requests converge.
	// Empty inherits publish.targets.
	Targets []string `yaml:"targets,omitempty" json:"targets,omitempty"`
	// Window is the reconcile period (default 12h).
	Window string `yaml:"window,omitempty" json:"window,omitempty"`
	// Anchor is the first publish time of a day, as HH:MM in Timezone
	// (default "02:00").
	Anchor string `yaml:"anchor,omitempty" json:"anchor,omitempty"`
	// Timezone is the IANA zone the anchor is interpreted in (default UTC).
	Timezone string `yaml:"timezone,omitempty" json:"timezone,omitempty"`
	// RetryAfter is the short backoff after the first failure of a version
	// (default 5m). Later failures advance by one window instead.
	RetryAfter string `yaml:"retry_after,omitempty" json:"retry_after,omitempty"`
	// Jitter is the maximum stable per-package offset inside a window
	// (default 30m), so a batch of nightly packages does not hit the lane at
	// the same instant.
	Jitter string `yaml:"jitter,omitempty" json:"jitter,omitempty"`
	// TickInterval is how often the reconciler looks for due rows
	// (default 1m).
	TickInterval string `yaml:"tick_interval,omitempty" json:"tick_interval,omitempty"`
	// Batch bounds how many rows one tick claims (default 10).
	Batch int `yaml:"batch,omitempty" json:"batch,omitempty"`
	// OnFailure is "alert" (default): a failed publication is recorded and
	// alerted, and the next window retries the same newest version. The
	// rollback strategy is not implemented by this track.
	OnFailure string `yaml:"on_failure,omitempty" json:"on_failure,omitempty"`
}

// EnabledValue reports whether convergence is switched on. Unset means off so
// that deploying the code does not silently change nightly behaviour; the
// staging (then prod) deployment enables it explicitly.
func (n NightlyPublish) EnabledValue() bool {
	return n.Enabled != nil && *n.Enabled
}

// WindowDuration returns the parsed window.
func (n NightlyPublish) WindowDuration() time.Duration {
	return parseNightlyDuration(n.Window, DefaultNightlyWindow)
}

// AnchorOffset returns the anchor as an offset from local midnight.
func (n NightlyPublish) AnchorOffset() time.Duration {
	offset, err := ParseNightlyAnchor(n.Anchor)
	if err != nil {
		return 2 * time.Hour
	}
	return offset
}

// Location returns the configured zone, defaulting to UTC.
func (n NightlyPublish) Location() *time.Location {
	if n.Timezone == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(n.Timezone)
	if err != nil {
		return time.UTC
	}
	return loc
}

// RetryAfterDuration returns the first-failure backoff.
func (n NightlyPublish) RetryAfterDuration() time.Duration {
	return parseNightlyDuration(n.RetryAfter, DefaultNightlyRetryAfter)
}

// JitterDuration returns the maximum jitter, capped below one window.
func (n NightlyPublish) JitterDuration() time.Duration {
	jitter := parseNightlyDuration(n.Jitter, DefaultNightlyJitter)
	if window := n.WindowDuration(); jitter >= window {
		return window / 2
	}
	return jitter
}

// TickEvery returns the reconciler interval.
func (n NightlyPublish) TickEvery() time.Duration {
	return parseNightlyDuration(n.TickInterval, DefaultNightlyTick)
}

// BatchSize returns how many due rows one tick claims.
func (n NightlyPublish) BatchSize() int {
	if n.Batch <= 0 {
		return DefaultNightlyBatch
	}
	return n.Batch
}

// DesiredTargets resolves the targets that use desired-state reconciliation.
// It returns nil when the feature is off, so callers can keep the legacy path.
func (n NightlyPublish) DesiredTargets(publishTargets []string) []string {
	if !n.EnabledValue() {
		return nil
	}
	if len(n.Targets) > 0 {
		return append([]string(nil), n.Targets...)
	}
	return append([]string(nil), publishTargets...)
}

// WantsTarget reports whether target converges instead of publishing at once.
func (n NightlyPublish) WantsTarget(target string, publishTargets []string) bool {
	for _, candidate := range n.DesiredTargets(publishTargets) {
		if candidate == target || candidate == "*" {
			return true
		}
	}
	return false
}

// ParseNightlyAnchor parses "HH:MM" or "HH:MM:SS" into an offset from
// midnight.
func ParseNightlyAnchor(value string) (time.Duration, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		value = DefaultNightlyAnchor
	}
	for _, layout := range []string{"15:04", "15:04:05"} {
		if parsed, err := time.Parse(layout, value); err == nil {
			return time.Duration(parsed.Hour())*time.Hour +
				time.Duration(parsed.Minute())*time.Minute +
				time.Duration(parsed.Second())*time.Second, nil
		}
	}
	return 0, fmt.Errorf("invalid nightly.anchor %q: want HH:MM", value)
}

// Validate checks the nightly configuration. It only rejects values that would
// silently misbehave; an unset section is valid (feature off).
func (n NightlyPublish) Validate() error {
	if !n.EnabledValue() {
		return nil
	}
	for _, field := range []struct {
		name  string
		value string
	}{
		{"nightly.window", n.Window},
		{"nightly.retry_after", n.RetryAfter},
		{"nightly.jitter", n.Jitter},
		{"nightly.tick_interval", n.TickInterval},
	} {
		if err := ValidateOptionalDuration(field.name, field.value); err != nil {
			return err
		}
	}
	if _, err := ParseNightlyAnchor(n.Anchor); err != nil {
		return err
	}
	if n.Timezone != "" {
		if _, err := time.LoadLocation(n.Timezone); err != nil {
			return fmt.Errorf("invalid nightly.timezone %q: %w", n.Timezone, err)
		}
	}
	jitter := DefaultNightlyJitter
	if n.Jitter != "" {
		parsed, err := time.ParseDuration(n.Jitter)
		if err != nil {
			return fmt.Errorf("invalid nightly.jitter %q: %w", n.Jitter, err)
		}
		jitter = parsed
	}
	if jitter >= n.WindowDuration() {
		return fmt.Errorf("invalid nightly.jitter %q: must be shorter than one window", n.Jitter)
	}
	if n.Batch < 0 {
		return fmt.Errorf("invalid nightly.batch %d: must be at least 1", n.Batch)
	}
	switch n.OnFailure {
	case "", "alert":
	case "rollback":
		return fmt.Errorf("nightly.on_failure=rollback is not implemented; use \"alert\"")
	default:
		return fmt.Errorf("invalid nightly.on_failure %q: want alert", n.OnFailure)
	}
	return nil
}

// ValidateOptionalDuration rejects malformed or non-positive durations while
// allowing an omitted value (the caller applies the default).
func ValidateOptionalDuration(name, value string) error {
	if value == "" {
		return nil
	}
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		return fmt.Errorf("invalid %s %q: must be a positive duration", name, value)
	}
	return nil
}

func parseNightlyDuration(value string, fallback time.Duration) time.Duration {
	if value == "" {
		return fallback
	}
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		return fallback
	}
	return d
}
