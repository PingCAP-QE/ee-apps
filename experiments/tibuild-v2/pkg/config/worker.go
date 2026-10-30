package config

import (
	"fmt"
	"os"
	"time"
)

const (
	defaultWorkerPollInterval    = 10 * time.Second
	defaultWorkerLease           = 90 * time.Second
	defaultWorkerHeartbeat       = 30 * time.Second
	defaultWorkerMaxAttempts     = 3
	defaultWorkerMetricsInterval = 15 * time.Second
	defaultWorkerReapInterval    = 30 * time.Second
	defaultWorkerBackoffBase     = 10 * time.Second
	defaultWorkerBackoffMax      = 5 * time.Minute
	defaultWorkerBacklogAfter    = 10 * time.Minute
	defaultOldestPendingAfter    = 30 * time.Minute
	defaultLeaseReclaimWindow    = 5 * time.Minute
	defaultWorkerAlertDedupe     = time.Hour
	// wildcardTarget lets a worker claim tasks for every target. It is the
	// only target accepted while the global target catalogue does not exist.
	wildcardTarget = "*"
)

// Worker configures the background task executor. The same configuration is
// used by the in-process worker (cmd/tibuild) and the standalone binary
// (cmd/tibuild-worker).
type Worker struct {
	// ID identifies this worker instance and is written to claimed_by.
	ID string `yaml:"id,omitempty" json:"id,omitempty"`
	// Targets restricts which target keys this worker claims. Empty means all.
	Targets []string `yaml:"targets,omitempty" json:"targets,omitempty"`
	// Concurrency is the number of parallel claim loops.
	Concurrency int `yaml:"concurrency,omitempty" json:"concurrency,omitempty"`
	// PollInterval is the idle polling interval (e.g. "10s").
	PollInterval string `yaml:"poll_interval,omitempty" json:"poll_interval,omitempty"`
	// Lease is how long a claim stays valid without a heartbeat (e.g. "90s").
	Lease string `yaml:"lease,omitempty" json:"lease,omitempty"`
	// Heartbeat is the lease renewal interval (e.g. "30s").
	Heartbeat string `yaml:"heartbeat,omitempty" json:"heartbeat,omitempty"`
	// HardTimeout bounds a single execution (e.g. "45s"). Empty disables it.
	// When set it must be less than lease*0.8.
	HardTimeout string `yaml:"hard_timeout,omitempty" json:"hard_timeout,omitempty"`
	// MaxAttempts is the number of attempts before a task is marked FAILED.
	MaxAttempts int `yaml:"max_attempts,omitempty" json:"max_attempts,omitempty"`
	// Kinds restricts which task kinds this worker executes.
	Kinds []string `yaml:"kinds,omitempty" json:"kinds,omitempty"`
	// MetricsInterval is how often task stats are recollected for metrics and
	// alerts (default: 15s).
	MetricsInterval string `yaml:"metrics_interval,omitempty" json:"metrics_interval,omitempty"`
	// ReapInterval is how often expired leases are reclaimed (default: 30s).
	ReapInterval string `yaml:"reap_interval,omitempty" json:"reap_interval,omitempty"`
	// BackoffBase is the first retry delay; it doubles per attempt
	// (default: 10s).
	BackoffBase string `yaml:"backoff_base,omitempty" json:"backoff_base,omitempty"`
	// BackoffMax caps the retry delay (default: 5m).
	BackoffMax string `yaml:"backoff_max,omitempty" json:"backoff_max,omitempty"`
	// Alerts configures the operational alerts derived from task stats.
	Alerts WorkerAlerts `yaml:"alerts,omitempty" json:"alerts,omitempty"`
}

// WorkerAlerts configures the alert thresholds. Empty durations keep the
// historical defaults, and a nil Enabled keeps alerting on.
type WorkerAlerts struct {
	// Enabled toggles alert delivery only; metrics keep refreshing.
	// It is a pointer so that an omitted key means true while an explicit
	// "enabled: false" is preserved.
	Enabled *bool `yaml:"enabled,omitempty" json:"enabled,omitempty"`
	// BacklogAfter is how long a target may stay backlogged before alerting.
	BacklogAfter string `yaml:"backlog_after,omitempty" json:"backlog_after,omitempty"`
	// OldestPendingAfter is the age of the oldest PENDING task that triggers an
	// alert.
	OldestPendingAfter string `yaml:"oldest_pending_after,omitempty" json:"oldest_pending_after,omitempty"`
	// LeaseReclaimWindow is how long after a reaper event the lease alert fires.
	LeaseReclaimWindow string `yaml:"lease_reclaim_window,omitempty" json:"lease_reclaim_window,omitempty"`
	// Dedupe is the minimum interval between two alerts with the same key.
	Dedupe string `yaml:"dedupe,omitempty" json:"dedupe,omitempty"`
}

// ApplyDefaults fills zero values with the documented defaults.
func (a *WorkerAlerts) ApplyDefaults() {
	if a.Enabled == nil {
		enabled := true
		a.Enabled = &enabled
	}
	if a.BacklogAfter == "" {
		a.BacklogAfter = defaultWorkerBacklogAfter.String()
	}
	if a.OldestPendingAfter == "" {
		a.OldestPendingAfter = defaultOldestPendingAfter.String()
	}
	if a.LeaseReclaimWindow == "" {
		a.LeaseReclaimWindow = defaultLeaseReclaimWindow.String()
	}
	if a.Dedupe == "" {
		a.Dedupe = defaultWorkerAlertDedupe.String()
	}
}

// ApplyDefaults fills zero values with the defaults documented in the spec.
func (w *Worker) ApplyDefaults() {
	if w.ID == "" {
		if hostname, err := os.Hostname(); err == nil && hostname != "" {
			w.ID = hostname
		} else {
			w.ID = "tibuild-worker"
		}
	}
	if len(w.Targets) == 0 {
		w.Targets = []string{wildcardTarget}
	}
	if w.Concurrency <= 0 {
		w.Concurrency = 1
	}
	if w.PollInterval == "" {
		w.PollInterval = defaultWorkerPollInterval.String()
	}
	if w.Lease == "" {
		w.Lease = defaultWorkerLease.String()
	}
	if w.Heartbeat == "" {
		w.Heartbeat = defaultWorkerHeartbeat.String()
	}
	if w.MaxAttempts <= 0 {
		w.MaxAttempts = defaultWorkerMaxAttempts
	}
	if len(w.Kinds) == 0 {
		w.Kinds = []string{"image_copy"}
	}
	if w.MetricsInterval == "" {
		w.MetricsInterval = defaultWorkerMetricsInterval.String()
	}
	if w.ReapInterval == "" {
		w.ReapInterval = defaultWorkerReapInterval.String()
	}
	if w.BackoffBase == "" {
		w.BackoffBase = defaultWorkerBackoffBase.String()
	}
	if w.BackoffMax == "" {
		w.BackoffMax = defaultWorkerBackoffMax.String()
	}
	w.Alerts.ApplyDefaults()
}

// Validate checks the worker configuration. knownTargets is the global target
// catalogue; pass nil while only the wildcard target is supported.
func (w Worker) Validate(knownTargets []string) error {
	normalized := w
	normalized.ApplyDefaults()

	poll, err := time.ParseDuration(normalized.PollInterval)
	if err != nil || poll <= 0 {
		return fmt.Errorf("invalid worker.poll_interval %q: must be a positive duration", normalized.PollInterval)
	}
	lease, err := time.ParseDuration(normalized.Lease)
	if err != nil || lease <= 0 {
		return fmt.Errorf("invalid worker.lease %q: must be a positive duration", normalized.Lease)
	}
	heartbeat, err := time.ParseDuration(normalized.Heartbeat)
	if err != nil || heartbeat <= 0 {
		return fmt.Errorf("invalid worker.heartbeat %q: must be a positive duration", normalized.Heartbeat)
	}
	if lease < 2*heartbeat {
		return fmt.Errorf("invalid worker lease: lease %s must be at least 2x heartbeat %s", lease, heartbeat)
	}
	if normalized.HardTimeout != "" {
		hardTimeout, err := time.ParseDuration(normalized.HardTimeout)
		if err != nil || hardTimeout <= 0 {
			return fmt.Errorf("invalid worker.hard_timeout %q: must be a positive duration", normalized.HardTimeout)
		}
		if hardTimeout >= lease*4/5 {
			return fmt.Errorf("invalid worker.hard_timeout %s: must be less than lease*0.8 (%s)", hardTimeout, lease*4/5)
		}
	}
	if normalized.Concurrency < 1 {
		return fmt.Errorf("invalid worker.concurrency %d: must be at least 1", normalized.Concurrency)
	}
	if normalized.MaxAttempts < 1 {
		return fmt.Errorf("invalid worker.max_attempts %d: must be at least 1", normalized.MaxAttempts)
	}
	if len(normalized.Kinds) == 0 {
		return fmt.Errorf("invalid worker.kinds: at least one kind is required")
	}
	for _, field := range []struct {
		name  string
		value string
	}{
		{"worker.metrics_interval", normalized.MetricsInterval},
		{"worker.reap_interval", normalized.ReapInterval},
		{"worker.backoff_base", normalized.BackoffBase},
		{"worker.backoff_max", normalized.BackoffMax},
		{"worker.alerts.backlog_after", normalized.Alerts.BacklogAfter},
		{"worker.alerts.oldest_pending_after", normalized.Alerts.OldestPendingAfter},
		{"worker.alerts.lease_reclaim_window", normalized.Alerts.LeaseReclaimWindow},
		{"worker.alerts.dedupe", normalized.Alerts.Dedupe},
	} {
		if err := validatePositiveDuration(field.name, field.value); err != nil {
			return err
		}
	}

	return validateTargets(normalized.Targets, knownTargets)
}

// validatePositiveDuration rejects malformed and non-positive durations. Zero
// and negative values would otherwise be silently replaced by the fallback.
func validatePositiveDuration(name, value string) error {
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		return fmt.Errorf("invalid %s %q: must be a positive duration", name, value)
	}
	return nil
}

func validateTargets(targets, knownTargets []string) error {
	known := make(map[string]struct{}, len(knownTargets))
	for _, target := range knownTargets {
		known[target] = struct{}{}
	}

	hasWildcard := false
	for _, target := range targets {
		if target == wildcardTarget {
			hasWildcard = true
			continue
		}
		if _, ok := known[target]; !ok {
			return fmt.Errorf("unknown worker target %q: not in the global target catalogue", target)
		}
	}
	if hasWildcard && len(targets) > 1 {
		return fmt.Errorf("worker.targets must not mix %q with concrete targets", wildcardTarget)
	}
	return nil
}

// PollEvery returns the parsed idle polling interval.
func (w Worker) PollEvery() time.Duration {
	return parseWorkerDuration(w.PollInterval, defaultWorkerPollInterval)
}

// LeaseDuration returns the parsed lease duration.
func (w Worker) LeaseDuration() time.Duration {
	return parseWorkerDuration(w.Lease, defaultWorkerLease)
}

// HeartbeatDuration returns the parsed heartbeat interval.
func (w Worker) HeartbeatDuration() time.Duration {
	return parseWorkerDuration(w.Heartbeat, defaultWorkerHeartbeat)
}

// HardTimeoutDuration returns the parsed execution timeout. Zero means
// disabled.
func (w Worker) HardTimeoutDuration() time.Duration {
	return parseWorkerDuration(w.HardTimeout, 0)
}

// MetricsEvery returns how often task stats are recollected for metrics and
// alerts.
func (w Worker) MetricsEvery() time.Duration {
	return parseWorkerDuration(w.MetricsInterval, defaultWorkerMetricsInterval)
}

// ReapEvery returns how often expired leases are reclaimed.
func (w Worker) ReapEvery() time.Duration {
	return parseWorkerDuration(w.ReapInterval, defaultWorkerReapInterval)
}

// BackoffBaseDuration returns the first retry delay.
func (w Worker) BackoffBaseDuration() time.Duration {
	return parseWorkerDuration(w.BackoffBase, defaultWorkerBackoffBase)
}

// BackoffMaxDuration returns the retry delay cap.
func (w Worker) BackoffMaxDuration() time.Duration {
	return parseWorkerDuration(w.BackoffMax, defaultWorkerBackoffMax)
}

// AlertsEnabled reports whether alerts are delivered. An unset value means on.
func (w Worker) AlertsEnabled() bool {
	if w.Alerts.Enabled == nil {
		return true
	}
	return *w.Alerts.Enabled
}

// AlertBacklogAfter returns how long a target may stay backlogged before
// alerting.
func (w Worker) AlertBacklogAfter() time.Duration {
	return parseWorkerDuration(w.Alerts.BacklogAfter, defaultWorkerBacklogAfter)
}

// AlertOldestPendingAfter returns the age of the oldest PENDING task that
// triggers an alert.
func (w Worker) AlertOldestPendingAfter() time.Duration {
	return parseWorkerDuration(w.Alerts.OldestPendingAfter, defaultOldestPendingAfter)
}

// AlertLeaseReclaimWindow returns how long after a reaper event the lease alert
// still fires.
func (w Worker) AlertLeaseReclaimWindow() time.Duration {
	return parseWorkerDuration(w.Alerts.LeaseReclaimWindow, defaultLeaseReclaimWindow)
}

// AlertDedupe returns the minimum interval between two alerts with the same
// key.
func (w Worker) AlertDedupe() time.Duration {
	return parseWorkerDuration(w.Alerts.Dedupe, defaultWorkerAlertDedupe)
}

func parseWorkerDuration(value string, fallback time.Duration) time.Duration {
	if value == "" {
		return fallback
	}
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		return fallback
	}
	return d
}
