package config

import (
	"fmt"
	"sort"
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
// Default supplies the schedule for every target; Targets overrides individual
// fields per target. This is what lets staging converge hourly while prod
// converges twice a day without code changes.
type NightlyPublish struct {
	// Enabled switches the listed targets to desired-state reconciliation. It
	// is a pointer so an omitted key means "keep the legacy behaviour" while
	// an explicit `enabled: false` is preserved.
	Enabled *bool `yaml:"enabled,omitempty" json:"enabled,omitempty"`
	// TickInterval is how often the reconciler looks for due rows
	// (default 1m).
	TickInterval string `yaml:"tick_interval,omitempty" json:"tick_interval,omitempty"`
	// Batch bounds how many rows one tick claims (default 10).
	Batch int `yaml:"batch,omitempty" json:"batch,omitempty"`
	// Default is the schedule for targets without an explicit override.
	Default NightlyWindow `yaml:"default,omitempty" json:"default,omitempty"`
	// Targets maps a publish target to its schedule override. Empty targets
	// inherits publish.targets when convergence is enabled.
	Targets map[string]NightlyWindow `yaml:"targets,omitempty" json:"targets,omitempty"`
}

// NightlyWindow is one target's converge schedule. Empty fields inherit the
// parent NightlyPublish.Default.
type NightlyWindow struct {
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
	// (default 30m, capped at window/4), so a batch of nightly packages does
	// not hit the lane at the same instant.
	Jitter string `yaml:"jitter,omitempty" json:"jitter,omitempty"`
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

// WindowFor returns the target's effective schedule: the default merged with
// the target override, field by field.
func (n NightlyPublish) WindowFor(target string) NightlyWindow {
	merged := n.Default
	override, ok := n.Targets[target]
	if !ok {
		return merged
	}
	if override.Window != "" {
		merged.Window = override.Window
	}
	if override.Anchor != "" {
		merged.Anchor = override.Anchor
	}
	if override.Timezone != "" {
		merged.Timezone = override.Timezone
	}
	if override.RetryAfter != "" {
		merged.RetryAfter = override.RetryAfter
	}
	if override.Jitter != "" {
		merged.Jitter = override.Jitter
	}
	if override.OnFailure != "" {
		merged.OnFailure = override.OnFailure
	}
	return merged
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
// Explicit target names are sorted so startup logs and tests are deterministic.
func (n NightlyPublish) DesiredTargets(publishTargets []string) []string {
	if !n.EnabledValue() {
		return nil
	}
	if len(n.Targets) > 0 {
		targets := make([]string, 0, len(n.Targets))
		for target := range n.Targets {
			targets = append(targets, target)
		}
		sort.Strings(targets)
		return targets
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

// WindowDuration returns the parsed window.
func (w NightlyWindow) WindowDuration() time.Duration {
	return parseNightlyDuration(w.Window, DefaultNightlyWindow)
}

// AnchorOffset returns the anchor as an offset from local midnight.
func (w NightlyWindow) AnchorOffset() time.Duration {
	offset, err := ParseNightlyAnchor(w.Anchor)
	if err != nil {
		return 2 * time.Hour
	}
	return offset
}

// Location returns the configured zone, defaulting to UTC.
func (w NightlyWindow) Location() *time.Location {
	if w.Timezone == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(w.Timezone)
	if err != nil {
		return time.UTC
	}
	return loc
}

// RetryAfterDuration returns the first-failure backoff.
func (w NightlyWindow) RetryAfterDuration() time.Duration {
	return parseNightlyDuration(w.RetryAfter, DefaultNightlyRetryAfter)
}

// JitterDuration returns the maximum jitter, capped below one window. The cap
// is window/4 (at most the 30m default): a one-hour staging window should not
// spread packages over half an hour.
func (w NightlyWindow) JitterDuration() time.Duration {
	jitter := parseNightlyDuration(w.Jitter, DefaultNightlyJitter)
	limit := w.WindowDuration() / 4
	if limit > DefaultNightlyJitter {
		limit = DefaultNightlyJitter
	}
	if jitter > limit {
		return limit
	}
	return jitter
}

// Validate checks one schedule. It only rejects values that would silently
// misbehave; an empty schedule is valid and gets the defaults.
func (w NightlyWindow) Validate(prefix string) error {
	for _, field := range []struct {
		name  string
		value string
	}{
		{prefix + ".window", w.Window},
		{prefix + ".retry_after", w.RetryAfter},
		{prefix + ".jitter", w.Jitter},
	} {
		if err := ValidateOptionalDuration(field.name, field.value); err != nil {
			return err
		}
	}
	if _, err := ParseNightlyAnchor(w.Anchor); err != nil {
		return fmt.Errorf("invalid %s.anchor: %w", prefix, err)
	}
	if w.Timezone != "" {
		if _, err := time.LoadLocation(w.Timezone); err != nil {
			return fmt.Errorf("invalid %s.timezone %q: %w", prefix, w.Timezone, err)
		}
	}
	jitter := DefaultNightlyJitter
	if w.Jitter != "" {
		parsed, err := time.ParseDuration(w.Jitter)
		if err != nil {
			return fmt.Errorf("invalid %s.jitter %q: %w", prefix, w.Jitter, err)
		}
		jitter = parsed
	}
	if jitter >= w.WindowDuration() {
		return fmt.Errorf("invalid %s.jitter %q: must be shorter than one window", prefix, w.Jitter)
	}
	switch w.OnFailure {
	case "", "alert":
	case "rollback":
		return fmt.Errorf("%s.on_failure=rollback is not implemented; use \"alert\"", prefix)
	default:
		return fmt.Errorf("invalid %s.on_failure %q: want alert", prefix, w.OnFailure)
	}
	return nil
}

// Validate checks the nightly configuration. It only rejects values that would
// silently misbehave; an unset section is valid (feature off).
func (n NightlyPublish) Validate() error {
	if !n.EnabledValue() {
		return nil
	}
	if err := ValidateOptionalDuration("nightly.tick_interval", n.TickInterval); err != nil {
		return err
	}
	if n.Batch < 0 {
		return fmt.Errorf("invalid nightly.batch %d: must be at least 1", n.Batch)
	}
	if err := n.Default.Validate("nightly.default"); err != nil {
		return err
	}
	for target, window := range n.Targets {
		if strings.TrimSpace(target) == "" {
			return fmt.Errorf("invalid nightly.targets: empty target name")
		}
		if err := window.Validate("nightly.targets." + target); err != nil {
			return err
		}
	}
	return nil
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
