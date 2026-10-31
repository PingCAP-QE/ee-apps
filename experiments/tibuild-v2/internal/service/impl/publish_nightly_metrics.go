package impl

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// nightlyMetrics exposes the desired-state queue to Prometheus. It is merged
// with the image-sync metrics by compositeWorker, so one /metrics endpoint
// serves both queues.
type nightlyMetrics struct {
	registry  *prometheus.Registry
	due       *prometheus.CounterVec
	lag       *prometheus.GaugeVec
	published *prometheus.CounterVec
	laneBusy  *prometheus.CounterVec
	laneWait  *prometheus.GaugeVec
}

func newNightlyMetrics() *nightlyMetrics {
	m := &nightlyMetrics{
		registry: prometheus.NewRegistry(),
		due: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "publish_desired_due_total",
			Help: "Number of due nightly desired-state rows claimed by this worker.",
		}, []string{"target"}),
		lag: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_desired_lag_seconds",
			Help: "Age of the oldest unpublished desired artifact per target.",
		}, []string{"target"}),
		published: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "nightly_publish_total",
			Help: "Number of settled nightly publish units by result.",
		}, []string{"result"}),
		laneBusy: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "publish_lane_busy_total",
			Help: "Number of TiUP steps requeued because another unit holds the publish lane.",
		}, []string{"target"}),
		laneWait: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_lane_wait_seconds",
			Help: "Seconds spent waiting for the publish lane by the step that last found it busy, per target.",
		}, []string{"target"}),
	}
	m.registry.MustRegister(m.due, m.lag, m.published, m.laneBusy, m.laneWait)
	return m
}

func (m *nightlyMetrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

func (m *nightlyMetrics) Gatherer() prometheus.Gatherer {
	return m.registry
}

// MetricsHandler exposes the nightly metrics for a standalone worker. The
// composite worker merges the gatherers instead, so /metrics carries both the
// image-sync and nightly series.
func (s *publishsrvc) MetricsHandler() http.Handler {
	if s.metrics == nil {
		return nil
	}
	return s.metrics.Handler()
}

// MetricsGatherer lets compositeWorker merge this registry with the artifact
// worker's registry.
func (s *publishsrvc) MetricsGatherer() prometheus.Gatherer {
	if s.metrics == nil {
		return nil
	}
	return s.metrics.Gatherer()
}

func (m *nightlyMetrics) recordDue(target string) {
	m.due.WithLabelValues(target).Inc()
}

func (m *nightlyMetrics) recordPublished(result string) {
	m.published.WithLabelValues(result).Inc()
}

func (m *nightlyMetrics) recordLaneBusy(target string) {
	m.laneBusy.WithLabelValues(target).Inc()
}

// recordLaneWait publishes how long the requeued step has been waiting for the
// lane. Each busy event overwrites the previous value: the gauge is the current
// wait of a target, not a lifetime maximum.
func (m *nightlyMetrics) recordLaneWait(target string, waited time.Duration) {
	if waited < 0 {
		waited = 0
	}
	m.laneWait.WithLabelValues(target).Set(waited.Seconds())
}

// refresh recomputes the lag gauge from rows whose desired version has not
// been published yet. A succeeded version is not lagging even if its
// desired_at is old.
func (m *nightlyMetrics) refresh(ctx context.Context, db *ent.Client, now time.Time) {
	rows, err := db.PublishDesired.Query().All(ctx)
	if err != nil {
		return
	}
	maxLag := map[string]time.Duration{}
	for _, row := range rows {
		if row.LastSucceededVersion != "" && row.LastSucceededVersion == row.DesiredVersion {
			continue
		}
		lag := now.Sub(row.DesiredAt)
		if lag < 0 {
			lag = 0
		}
		if lag > maxLag[row.Target] {
			maxLag[row.Target] = lag
		}
	}
	m.lag.Reset()
	for target, lag := range maxLag {
		m.lag.WithLabelValues(target).Set(lag.Seconds())
	}
}

// nightlyAlertTracker deduplicates the lag alerts a worker sends. The nightly
// reconciler runs on the TiUP workers; a single replica per target is the
// current deployment, so process-local dedupe is enough for this track.
type nightlyAlertTracker struct {
	mu       sync.Mutex
	dedupe   time.Duration
	lastSent map[string]time.Time
}

func newNightlyAlertTracker(dedupe time.Duration) *nightlyAlertTracker {
	if dedupe <= 0 {
		dedupe = time.Hour
	}
	return &nightlyAlertTracker{dedupe: dedupe, lastSent: map[string]time.Time{}}
}

// evaluate sends at most one alert per desired row per dedupe window. A row is
// lagging when its desired artifact is older than 1.5 windows and has not been
// published.
func (a *nightlyAlertTracker) evaluate(
	ctx context.Context,
	notifier AlertNotifier,
	rows []*ent.PublishDesired,
	now time.Time,
	windowFor func(string) time.Duration,
	logger *zerolog.Logger,
) {
	if notifier == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()

	for _, row := range rows {
		if row.LastSucceededVersion != "" && row.LastSucceededVersion == row.DesiredVersion {
			continue
		}
		window := windowFor(row.Target)
		lag := now.Sub(row.DesiredAt)
		if window <= 0 || lag <= window*3/2 {
			continue
		}
		key := fmt.Sprintf("%s/%s/%s/%s", row.Target, row.Pkg, row.Os, row.Arch)
		if last, ok := a.lastSent[key]; ok && now.Sub(last) < a.dedupe {
			continue
		}
		body := fmt.Sprintf(
			"target=%s package=%s os=%s arch=%s desired=%s lag=%s window=%s",
			row.Target, row.Pkg, row.Os, row.Arch, row.DesiredVersion,
			lag.Round(time.Minute), window,
		)
		if err := notifier.SendAlert(ctx, "[tibuild] nightly publish lag", body); err != nil {
			logger.Err(err).Str("key", key).Msg("failed to send the nightly lag alert")
			continue
		}
		a.lastSent[key] = now
	}
}

// refreshNightlyMetricsAndAlerts is called by the nightly tick. It keeps the
// lag gauge current and, when Lark is configured, alerts on rows that have
// been waiting for more than 1.5 windows.
func (s *publishsrvc) refreshNightlyMetricsAndAlerts(ctx context.Context) {
	if s.metrics == nil {
		return
	}
	now := s.now()
	s.metrics.refresh(ctx, s.db, now)

	if s.alerter == nil || s.nightlyAlerts == nil {
		return
	}
	rows, err := s.db.PublishDesired.Query().All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to scan nightly rows for lag alerts")
		return
	}
	s.nightlyAlerts.evaluate(ctx, s.alerter, rows, now, func(target string) time.Duration {
		return s.publishCfg.Nightly.WindowFor(target).WindowDuration()
	}, s.logger)
}
