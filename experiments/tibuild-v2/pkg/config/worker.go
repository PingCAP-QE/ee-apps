package config

import (
	"fmt"
	"os"
	"time"
)

const (
	defaultWorkerPollInterval = 10 * time.Second
	defaultWorkerLease        = 90 * time.Second
	defaultWorkerHeartbeat    = 30 * time.Second
	defaultWorkerMaxAttempts  = 3
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
	// LegacyStartupReclaim keeps the pre-lease behavior of resetting all
	// PROCESSING rows on startup. It exists as a rollback switch for Phase 1.
	LegacyStartupReclaim *bool `yaml:"legacy_startup_reclaim,omitempty" json:"legacy_startup_reclaim,omitempty"`
	// InProcess controls whether cmd/tibuild starts the worker in-process.
	InProcess *bool `yaml:"in_process,omitempty" json:"in_process,omitempty"`
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

	return validateTargets(normalized.Targets, knownTargets)
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

// LegacyReclaimEnabled reports whether startup must reset PROCESSING rows.
func (w Worker) LegacyReclaimEnabled() bool {
	return w.LegacyStartupReclaim == nil || *w.LegacyStartupReclaim
}

// InProcessEnabled reports whether cmd/tibuild runs the worker in-process.
func (w Worker) InProcessEnabled() bool {
	return w.InProcess == nil || *w.InProcess
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
