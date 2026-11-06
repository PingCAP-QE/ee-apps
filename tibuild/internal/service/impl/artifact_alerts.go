package impl

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// AlertNotifier sends a plain-text operational alert. LarkNotifier implements
// it, reusing the configured Lark app and channels.
type AlertNotifier interface {
	SendAlert(ctx context.Context, title, body string) error
}

// alertSettings are the alert thresholds resolved from worker.alerts. Defaults
// match the values that used to be hard-coded constants, so an untouched
// config keeps the historical behaviour.
type alertSettings struct {
	enabled            bool
	backlogAfter       time.Duration
	oldestPendingAfter time.Duration
	leaseReclaimWindow time.Duration
	dedupe             time.Duration
}

// newAlertSettings resolves the thresholds, applying defaults for empty fields.
func newAlertSettings(cfg config.Worker) alertSettings {
	cfg.ApplyDefaults()
	return alertSettings{
		enabled:            cfg.AlertsEnabled(),
		backlogAfter:       cfg.AlertBacklogAfter(),
		oldestPendingAfter: cfg.AlertOldestPendingAfter(),
		leaseReclaimWindow: cfg.AlertLeaseReclaimWindow(),
		dedupe:             cfg.AlertDedupe(),
	}
}

type alertTracker struct {
	mu               sync.Mutex
	settings         alertSettings
	backlogSince     map[string]time.Time
	lastLeaseReclaim time.Time
	lastSent         map[string]time.Time
}

type pendingAlert struct {
	key   string
	title string
	body  string
}

func newAlertTracker(settings alertSettings) *alertTracker {
	return &alertTracker{
		settings:     settings,
		backlogSince: make(map[string]time.Time),
		lastSent:     make(map[string]time.Time),
	}
}

// setSettings swaps the thresholds without dropping the dedupe state, so a
// config reload cannot re-send an alert that was just delivered.
func (a *alertTracker) setSettings(settings alertSettings) {
	a.mu.Lock()
	a.settings = settings
	a.mu.Unlock()
}

// isEnabled reports whether alert delivery is configured.
func (a *alertTracker) isEnabled() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.settings.enabled
}

// setLastLeaseReclaim records the shared reaper timestamp read from the
// alert_events table. The zero time means "no reclaim recorded".
func (a *alertTracker) setLastLeaseReclaim(at time.Time) {
	a.mu.Lock()
	a.lastLeaseReclaim = at
	a.mu.Unlock()
}

// evaluate turns the current task stats into at most one alert per condition
// and dedupe window. The notifier is called outside the lock.
func (a *alertTracker) evaluate(
	ctx context.Context,
	notifier AlertNotifier,
	stats map[string]taskStats,
	now time.Time,
	logger *zerolog.Logger,
) {
	if notifier == nil {
		return
	}

	a.mu.Lock()
	settings := a.settings
	if !settings.enabled {
		a.mu.Unlock()
		return
	}

	var pending []pendingAlert
	send := func(key, title, body string) {
		if last, ok := a.lastSent[key]; ok && now.Sub(last) < settings.dedupe {
			return
		}
		a.lastSent[key] = now
		pending = append(pending, pendingAlert{key: key, title: title, body: body})
	}

	for target, s := range stats {
		if s.Backlog > 0 {
			since, ok := a.backlogSince[target]
			if !ok {
				since = now
				a.backlogSince[target] = now
			}
			if now.Sub(since) >= settings.backlogAfter {
				send(
					"backlog:"+target,
					"[tibuild] publish backlog",
					fmt.Sprintf("target=%s backlog=%d pending_for=%s", target, s.Backlog, now.Sub(since).Round(time.Minute)),
				)
			}
		} else {
			delete(a.backlogSince, target)
		}

		if !s.OldestPending.IsZero() && now.Sub(s.OldestPending) >= settings.oldestPendingAfter {
			send(
				"oldest:"+target,
				"[tibuild] oldest pending task",
				fmt.Sprintf("target=%s oldest_pending=%s", target, now.Sub(s.OldestPending).Round(time.Minute)),
			)
		}
	}

	if settings.leaseReclaimWindow > 0 && !a.lastLeaseReclaim.IsZero() && now.Sub(a.lastLeaseReclaim) <= settings.leaseReclaimWindow {
		bucket := a.lastLeaseReclaim.Unix() / int64(settings.leaseReclaimWindow/time.Second)
		send(
			fmt.Sprintf("lease:%d", bucket),
			"[tibuild] task lease reclaimed",
			fmt.Sprintf("an expired task lease was reclaimed within the last %s (last_reclaim=%s)", settings.leaseReclaimWindow, a.lastLeaseReclaim.Format(time.RFC3339)),
		)
	}
	a.mu.Unlock()

	for _, alert := range pending {
		if err := notifier.SendAlert(ctx, alert.title, alert.body); err != nil {
			logger.Err(err).Str("alert", alert.key).Msg("failed to send lark alert")
		}
	}
}
