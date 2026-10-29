package impl

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/rs/zerolog"
)

const (
	backlogAlertAfter       = 10 * time.Minute
	oldestPendingAlertAfter = 30 * time.Minute
	leaseReclaimAlertWindow = 5 * time.Minute
	alertDedupeWindow       = time.Hour
)

// AlertNotifier sends a plain-text operational alert. LarkNotifier implements
// it, reusing the configured Lark app and channels.
type AlertNotifier interface {
	SendAlert(ctx context.Context, title, body string) error
}

type alertTracker struct {
	mu               sync.Mutex
	backlogSince     map[string]time.Time
	lastLeaseReclaim time.Time
	lastSent         map[string]time.Time
}

type pendingAlert struct {
	key   string
	title string
	body  string
}

func newAlertTracker() *alertTracker {
	return &alertTracker{
		backlogSince: make(map[string]time.Time),
		lastSent:     make(map[string]time.Time),
	}
}

// noteLeaseReclaim records a reaper event for the lease alert.
func (a *alertTracker) noteLeaseReclaim(n int, now time.Time) {
	if n <= 0 {
		return
	}
	a.mu.Lock()
	a.lastLeaseReclaim = now
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
	var pending []pendingAlert
	send := func(key, title, body string) {
		if last, ok := a.lastSent[key]; ok && now.Sub(last) < alertDedupeWindow {
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
			if now.Sub(since) >= backlogAlertAfter {
				send(
					"backlog:"+target,
					"[tibuild] publish backlog",
					fmt.Sprintf("target=%s backlog=%d pending_for=%s", target, s.Backlog, now.Sub(since).Round(time.Minute)),
				)
			}
		} else {
			delete(a.backlogSince, target)
		}

		if !s.OldestPending.IsZero() && now.Sub(s.OldestPending) >= oldestPendingAlertAfter {
			send(
				"oldest:"+target,
				"[tibuild] oldest pending task",
				fmt.Sprintf("target=%s oldest_pending=%s", target, now.Sub(s.OldestPending).Round(time.Minute)),
			)
		}
	}

	if !a.lastLeaseReclaim.IsZero() && now.Sub(a.lastLeaseReclaim) <= leaseReclaimAlertWindow {
		bucket := a.lastLeaseReclaim.Unix() / int64(leaseReclaimAlertWindow/time.Second)
		send(
			fmt.Sprintf("lease:%d", bucket),
			"[tibuild] task lease reclaimed",
			fmt.Sprintf("an expired task lease was reclaimed within the last %s (last_reclaim=%s)", leaseReclaimAlertWindow, a.lastLeaseReclaim.Format(time.RFC3339)),
		)
	}
	a.mu.Unlock()

	for _, alert := range pending {
		if err := notifier.SendAlert(ctx, alert.title, alert.body); err != nil {
			logger.Err(err).Str("alert", alert.key).Msg("failed to send lark alert")
		}
	}
}
