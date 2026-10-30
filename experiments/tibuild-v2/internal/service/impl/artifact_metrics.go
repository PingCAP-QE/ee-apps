package impl

import (
	"context"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
)

// taskStats is the per-target aggregation used by metrics and alerts.
type taskStats struct {
	Backlog       int
	Inflight      int
	OldestPending time.Time
}

type artifactMetrics struct {
	registry     *prometheus.Registry
	backlog      *prometheus.GaugeVec
	oldest       *prometheus.GaugeVec
	inflight     *prometheus.GaugeVec
	leaseExpired prometheus.Counter
	failed       *prometheus.CounterVec
}

func newArtifactMetrics() *artifactMetrics {
	m := &artifactMetrics{
		registry: prometheus.NewRegistry(),
		backlog: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_task_backlog",
			Help: "Number of PENDING image sync tasks per target.",
		}, []string{"target"}),
		oldest: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_task_oldest_pending_seconds",
			Help: "Age of the oldest PENDING image sync task per target.",
		}, []string{"target"}),
		inflight: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_task_inflight",
			Help: "Number of PROCESSING image sync tasks per target.",
		}, []string{"target"}),
		leaseExpired: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "publish_task_lease_expired_total",
			Help: "Number of image sync tasks reclaimed after their lease expired.",
		}),
		failed: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "publish_task_failed_total",
			Help: "Number of image sync tasks that reached FAILED.",
		}, []string{"target", "reason"}),
	}
	m.registry.MustRegister(m.backlog, m.oldest, m.inflight, m.leaseExpired, m.failed)
	return m
}

func (m *artifactMetrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

func (m *artifactMetrics) refresh(stats map[string]taskStats, now time.Time) {
	m.backlog.Reset()
	m.oldest.Reset()
	m.inflight.Reset()

	for target, s := range stats {
		m.backlog.WithLabelValues(target).Set(float64(s.Backlog))
		m.inflight.WithLabelValues(target).Set(float64(s.Inflight))
		oldest := 0.0
		if !s.OldestPending.IsZero() {
			oldest = now.Sub(s.OldestPending).Seconds()
		}
		m.oldest.WithLabelValues(target).Set(oldest)
	}
}

func (m *artifactMetrics) recordReclaimed(n int) {
	if n > 0 {
		m.leaseExpired.Add(float64(n))
	}
}

func (m *artifactMetrics) recordFailed(target, reason string) {
	m.failed.WithLabelValues(target, reason).Inc()
}

func (s *artifactsrvc) ensureObservability() {
	if s.metrics == nil {
		s.metrics = newArtifactMetrics()
	}
	if s.alerts == nil {
		s.alerts = newAlertTracker(newAlertSettings(s.workerConfig()))
	}
}

func (s *artifactsrvc) MetricsHandler() http.Handler {
	s.ensureObservability()
	return s.metrics.Handler()
}

// collectTaskStats aggregates PENDING/PROCESSING tasks by target key.
func (s *artifactsrvc) collectTaskStats(ctx context.Context) (map[string]taskStats, error) {
	tasks, err := s.db.ImageSyncTask.Query().
		Where(entimagesynctask.StatusIn(TaskStatusPending, TaskStatusProcessing)).
		Select(
			entimagesynctask.FieldTargetKey,
			entimagesynctask.FieldStatus,
			entimagesynctask.FieldCreatedAt,
		).
		All(ctx)
	if err != nil {
		return nil, err
	}

	stats := make(map[string]taskStats)
	for _, task := range tasks {
		target := task.TargetKey
		if target == "" {
			target = "unknown"
		}
		entry := stats[target]
		switch task.Status {
		case TaskStatusPending:
			entry.Backlog++
			if entry.OldestPending.IsZero() || task.CreatedAt.Before(entry.OldestPending) {
				entry.OldestPending = task.CreatedAt
			}
		case TaskStatusProcessing:
			entry.Inflight++
		}
		stats[target] = entry
	}
	return stats, nil
}

func (s *artifactsrvc) runMetrics(ctx context.Context) {
	// MetricsEvery() always resolves to a positive interval, defaulting to 15s.
	ticker := time.NewTicker(s.workerConfig().MetricsEvery())
	defer ticker.Stop()

	s.refreshMetricsAndAlerts(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.refreshMetricsAndAlerts(ctx)
		}
	}
}

// alertNotifierWithRecipients is implemented by notifiers that can report
// whether they have anywhere to deliver.
type alertNotifierWithRecipients interface {
	HasRecipients() bool
}

// canEvaluateAlerts reports whether this worker may compete for the alert
// lease. A notifier without recipients (for example a cluster whose config has
// no lark.channels) is skipped so it cannot take the lease and drop alerts.
func (s *artifactsrvc) canEvaluateAlerts() bool {
	if s.alerter == nil {
		return false
	}
	if reporter, ok := s.alerter.(alertNotifierWithRecipients); ok && !reporter.HasRecipients() {
		return false
	}
	return true
}

func (s *artifactsrvc) refreshMetricsAndAlerts(ctx context.Context) {
	stats, err := s.collectTaskStats(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to collect image sync task metrics")
		return
	}
	now := s.now()
	s.ensureObservability()
	s.metrics.refresh(stats, now)

	if !s.alerts.isEnabled() {
		// Metrics keep refreshing; only alert delivery is disabled.
		return
	}
	if !s.canEvaluateAlerts() {
		// Metrics keep refreshing; this worker has no recipients to alert.
		return
	}
	// Every replica sees the same queue, so only the lease holder may evaluate.
	leader, err := s.acquireAlertLease(ctx, now)
	if err != nil {
		s.logger.Err(err).Msg("failed to acquire the alert lease")
		return
	}
	if !leader {
		return
	}
	s.alerts.evaluate(ctx, s.alerter, stats, now, s.logger)
}
