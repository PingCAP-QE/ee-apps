package impl

import (
	"context"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

type fakeAlertNotifier struct {
	mu     sync.Mutex
	alerts []string
}

func (f *fakeAlertNotifier) SendAlert(_ context.Context, title, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.alerts = append(f.alerts, title)
	return nil
}

func (f *fakeAlertNotifier) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.alerts)
}

func scrapeMetrics(t *testing.T, s *artifactsrvc) string {
	t.Helper()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "/metrics", nil)
	s.MetricsHandler().ServeHTTP(recorder, request)
	return recorder.Body.String()
}

func TestMetricsExposeTargetLabels(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a"})
	s.ensureObservability()

	old := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	_, err := old.Update().SetCreatedAt(time.Now().Add(-31 * time.Minute)).Save(ctx)
	require.NoError(t, err)
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	processing := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err = processing.Update().SetClaimedBy("worker-a").SetLeaseUntil(time.Now().Add(time.Minute)).Save(ctx)
	require.NoError(t, err)
	createLeaseTestTask(t, db, "gcr.io", TaskStatusPending)

	stats, err := s.collectTaskStats(ctx)
	require.NoError(t, err)
	s.metrics.refresh(stats, time.Now())
	s.metrics.recordReclaimed(2)

	body := scrapeMetrics(t, s)
	require.Contains(t, body, `publish_task_backlog{target="docker.io"} 2`)
	require.Contains(t, body, `publish_task_inflight{target="docker.io"} 1`)
	require.Contains(t, body, `publish_task_oldest_pending_seconds{target="docker.io"}`)
	require.Contains(t, body, `publish_task_backlog{target="gcr.io"} 1`)
	require.Contains(t, body, "publish_task_lease_expired_total 2")
}

func TestAlertsFireAfterThresholdsAndDedupe(t *testing.T) {
	now := time.Now()
	logger := zerolog.Nop()
	notifier := &fakeAlertNotifier{}
	tracker := newAlertTracker(newAlertSettings(config.Worker{}))

	stats := map[string]taskStats{
		"docker.io": {Backlog: 2, OldestPending: now.Add(-31 * time.Minute)},
	}
	tracker.evaluate(context.Background(), notifier, stats, now, &logger)
	require.Equal(t, 1, notifier.count(), "oldest-pending alert should fire immediately")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(11*time.Minute), &logger)
	require.Equal(t, 2, notifier.count(), "backlog alert should fire after 10 minutes")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(12*time.Minute), &logger)
	require.Equal(t, 2, notifier.count(), "alerts must be deduped within the window")

	empty := map[string]taskStats{"docker.io": {Backlog: 0}}
	tracker.evaluate(context.Background(), notifier, empty, now.Add(2*time.Hour), &logger)
	require.Equal(t, 2, notifier.count())
}

func TestLeaseReclaimAlertIsDedupedPerWindow(t *testing.T) {
	now := time.Now()
	logger := zerolog.Nop()
	notifier := &fakeAlertNotifier{}
	tracker := newAlertTracker(newAlertSettings(config.Worker{}))
	tracker.noteLeaseReclaim(3, now)

	tracker.evaluate(context.Background(), notifier, nil, now.Add(time.Minute), &logger)
	require.Equal(t, 1, notifier.count())

	tracker.evaluate(context.Background(), notifier, nil, now.Add(2*time.Minute), &logger)
	require.Equal(t, 1, notifier.count(), "same reclaim window must not alert twice")

	tracker.noteLeaseReclaim(1, now.Add(6*time.Minute))
	tracker.evaluate(context.Background(), notifier, nil, now.Add(6*time.Minute), &logger)
	require.Equal(t, 2, notifier.count(), "a new reclaim window should alert again")
}

func TestReapAndRecordIncrementsMetric(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "30s"})
	s.ensureObservability()

	task := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err := task.Update().
		SetClaimedBy("worker-old").
		SetLeaseUntil(time.Now().Add(-time.Minute)).
		Save(ctx)
	require.NoError(t, err)

	n, err := s.reapAndRecord(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, n)

	body := scrapeMetrics(t, s)
	require.Contains(t, body, "publish_task_lease_expired_total 1")
}

func TestHandleFailureRecordsFailedMetric(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", MaxAttempts: 3})
	s.ensureObservability()

	task := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err := task.Update().
		SetClaimedBy("worker-a").
		SetAttempts(3).
		SetLeaseUntil(time.Now().Add(time.Minute)).
		Save(ctx)
	require.NoError(t, err)
	task, err = db.ImageSyncTask.Get(ctx, task.ID)
	require.NoError(t, err)

	ok, err := s.handleFailure(ctx, task, context.DeadlineExceeded)
	require.NoError(t, err)
	require.True(t, ok)

	body := scrapeMetrics(t, s)
	require.Contains(t, body, `publish_task_failed_total{reason="max_attempts",target="docker.io"} 1`)
}

// The alert thresholds come from worker.alerts, so an e2e run can shorten them
// without changing code.
func TestAlertThresholdsFollowWorkerConfig(t *testing.T) {
	now := time.Now()
	logger := zerolog.Nop()
	notifier := &fakeAlertNotifier{}
	tracker := newAlertTracker(newAlertSettings(config.Worker{
		Alerts: config.WorkerAlerts{
			BacklogAfter:       "1m",
			OldestPendingAfter: "2m",
			LeaseReclaimWindow: "30s",
			Dedupe:             "5m",
		},
	}))

	stats := map[string]taskStats{
		"docker.io": {Backlog: 1, OldestPending: now.Add(-3 * time.Minute)},
	}
	tracker.evaluate(context.Background(), notifier, stats, now, &logger)
	require.Equal(t, 1, notifier.count(), "oldest-pending alert fires above the configured 2m")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(30*time.Second), &logger)
	require.Equal(t, 1, notifier.count(), "backlog must wait for the configured 1m")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(61*time.Second), &logger)
	require.Equal(t, 2, notifier.count(), "backlog alert fires after the configured 1m")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(4*time.Minute), &logger)
	require.Equal(t, 2, notifier.count(), "configured 5m dedupe still suppresses repeats")
}

func TestAlertsDisabledSkipsNotifications(t *testing.T) {
	now := time.Now()
	logger := zerolog.Nop()
	notifier := &fakeAlertNotifier{}
	disabled := false
	tracker := newAlertTracker(newAlertSettings(config.Worker{
		Alerts: config.WorkerAlerts{Enabled: &disabled},
	}))

	stats := map[string]taskStats{
		"docker.io": {Backlog: 5, OldestPending: now.Add(-2 * time.Hour)},
	}
	tracker.evaluate(context.Background(), notifier, stats, now, &logger)
	tracker.noteLeaseReclaim(2, now)
	tracker.evaluate(context.Background(), notifier, stats, now, &logger)
	require.Zero(t, notifier.count(), "alerts.enabled=false must suppress every alert")
}

// Disabling alerts must not stop the metrics refresh loop.
func TestAlertsDisabledStillRefreshesMetrics(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	disabled := false
	s := newLeaseTestService(t, db, config.Worker{
		ID:     "worker-a",
		Alerts: config.WorkerAlerts{Enabled: &disabled},
	})
	s.ensureObservability()
	notifier := &fakeAlertNotifier{}
	s.alerter = notifier
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)

	s.refreshMetricsAndAlerts(ctx)

	require.Zero(t, notifier.count())
	require.Contains(t, scrapeMetrics(t, s), `publish_task_backlog{target="docker.io"} 1`)
}

// A config reload refreshes the thresholds but must keep the dedupe state, so
// it cannot re-send an alert that was just delivered.
func TestAlertSettingsReloadKeepsDedupeState(t *testing.T) {
	now := time.Now()
	logger := zerolog.Nop()
	notifier := &fakeAlertNotifier{}
	tracker := newAlertTracker(newAlertSettings(config.Worker{}))
	stats := map[string]taskStats{
		"docker.io": {OldestPending: now.Add(-31 * time.Minute)},
	}

	tracker.evaluate(context.Background(), notifier, stats, now, &logger)
	require.Equal(t, 1, notifier.count())

	tracker.setSettings(newAlertSettings(config.Worker{
		Alerts: config.WorkerAlerts{Dedupe: "5m"},
	}))
	tracker.evaluate(context.Background(), notifier, stats, now.Add(time.Minute), &logger)
	require.Equal(t, 1, notifier.count(), "reload must not drop the dedupe state")

	tracker.evaluate(context.Background(), notifier, stats, now.Add(6*time.Minute), &logger)
	require.Equal(t, 2, notifier.count(), "the reloaded 5m window allows the next alert")
}
