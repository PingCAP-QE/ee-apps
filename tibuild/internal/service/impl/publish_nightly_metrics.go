package impl

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
)

// nightlyMetrics exposes the desired-state queue to Prometheus. It is merged
// with the image-sync metrics by compositeWorker, so one /metrics endpoint
// serves both queues.
type nightlyMetrics struct {
	registry   *prometheus.Registry
	due        *prometheus.CounterVec
	lag        *prometheus.GaugeVec
	published  *prometheus.CounterVec
	laneBusy   *prometheus.CounterVec
	laneWait   *prometheus.GaugeVec
	laneHold   *prometheus.HistogramVec
	starvation *prometheus.GaugeVec
	verify     *prometheus.CounterVec
	verifyTime *prometheus.GaugeVec
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
		laneHold: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "publish_lane_hold_seconds",
			Help:    "Seconds a TiUP step held the publish lane, per target.",
			Buckets: []float64{1, 5, 15, 30, 60, 120, 300, 600, 1200},
		}, []string{"target"}),
		starvation: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "publish_lane_starvation_seconds",
			Help: "Age of the oldest pending step per target and base priority.",
		}, []string{"priority", "target"}),
	}
	m.verify = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "publish_desired_verify_total",
		Help: "Nightly desired rows judged against the mirror metadata, by verification result.",
	}, []string{"mirror", "result"})
	m.verifyTime = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "publish_desired_verify_seconds",
		Help: "Seconds spent reading the mirror metadata for the last verification pass, per mirror.",
	}, []string{"mirror"})
	m.registry.MustRegister(m.due, m.lag, m.published, m.laneBusy, m.laneWait, m.laneHold, m.starvation, m.verify, m.verifyTime)
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

// recordVerify counts one verification outcome. The mirror label keeps a
// staging misconfiguration from hiding behind prod's successes.
func (m *nightlyMetrics) recordVerify(mirror, result string) {
	m.verify.WithLabelValues(mirror, result).Inc()
}

// observeVerify publishes the cost of the last verification pass per mirror.
func (m *nightlyMetrics) observeVerify(mirror string, took time.Duration) {
	if took < 0 {
		took = 0
	}
	m.verifyTime.WithLabelValues(mirror).Set(took.Seconds())
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

// refresh recomputes the lag gauge from the rows the mirror does not have yet.
// It takes the verification results so the gauge and the alert can never
// disagree about what "published" means.
func (m *nightlyMetrics) refresh(results []nightlyVerifyResult, now time.Time) {
	maxLag := map[string]time.Duration{}
	for _, result := range results {
		if result.Published {
			continue
		}
		row := result.Row
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

// recordLaneHold observes how long a step held the lane.
func (m *nightlyMetrics) recordLaneHold(target string, seconds float64) {
	if seconds < 0 {
		seconds = 0
	}
	m.laneHold.WithLabelValues(target).Observe(seconds)
}

// refreshStarvation exposes the oldest pending step per target and base
// priority. A rising nightly (priority 10) value is the signal that aging is
// not keeping up.
func (m *nightlyMetrics) refreshStarvation(ctx context.Context, db *ent.Client, now time.Time) {
	rows, err := db.PublishStep.Query().
		Where(entpublishstep.State(PublishStepStatePending)).
		All(ctx)
	if err != nil {
		return
	}
	oldest := map[string]map[int]time.Duration{}
	for _, row := range rows {
		wait := now.Sub(row.CreatedAt)
		if wait < 0 {
			wait = 0
		}
		byPriority := oldest[row.Target]
		if byPriority == nil {
			byPriority = map[int]time.Duration{}
			oldest[row.Target] = byPriority
		}
		if wait > byPriority[row.Priority] {
			byPriority[row.Priority] = wait
		}
	}
	m.starvation.Reset()
	for target, byPriority := range oldest {
		for priority, wait := range byPriority {
			m.starvation.WithLabelValues(strconv.Itoa(priority), target).Set(wait.Seconds())
		}
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
	results []nightlyVerifyResult,
	now time.Time,
	windowFor func(string) time.Duration,
	logger *zerolog.Logger,
) {
	if notifier == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()

	for _, result := range results {
		// Published is decided against the mirror's own metadata, so a channel
		// whose version string never changes still reports a stuck publication.
		if result.Published {
			continue
		}
		row := result.Row
		window := windowFor(row.Target)
		lag := now.Sub(row.DesiredAt)
		if window <= 0 || lag <= window*3/2 {
			continue
		}
		key := fmt.Sprintf("%s/%s/%s/%s", row.Target, row.Pkg, row.Os, row.Arch)
		if last, ok := a.lastSent[key]; ok && now.Sub(last) < a.dedupe {
			continue
		}
		if err := notifier.SendAlert(ctx, "[tibuild] nightly publish lag", nightlyLagBody(result, lag, window)); err != nil {
			logger.Err(err).Str("key", key).Msg("failed to send the nightly lag alert")
			continue
		}
		a.lastSent[key] = now
	}
}

// nightlyLagBody renders the alert. It names both digests, because the version
// string alone cannot tell two nightly builds apart, and the next scheduled
// attempt plus the last error, because "queued behind the lane" and "the mirror
// refused us" need different reactions.
func nightlyLagBody(result nightlyVerifyResult, lag, window time.Duration) string {
	row := result.Row
	body := fmt.Sprintf(
		"target=%s package=%s os=%s arch=%s\ndesired=%s sha256=%s\nmirror=%s\nlag=%s window=%s next_run_at=%s",
		row.Target, row.Pkg, row.Os, row.Arch,
		row.DesiredVersion, shortSHA256(desiredFileSHA256(row)),
		shortMirror(result), lag.Round(time.Minute), window,
		row.NextRunAt.UTC().Format(time.RFC3339),
	)
	if row.LastError != "" {
		body += "\nlast_error=" + truncateErrorMessage(row.LastError)
	}
	return body
}

// shortMirror names what the mirror holds: the published digest when it differs,
// or the fact that the entry is missing.
func shortMirror(result nightlyVerifyResult) string {
	if result.Result == verifyResultAbsent {
		return "absent"
	}
	if result.MirrorSHA == "" {
		return result.Result
	}
	return shortSHA256(result.MirrorSHA)
}

func shortSHA256(sha string) string {
	if sha == "" {
		return "unknown"
	}
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

// refreshNightlyMetricsAndAlerts is called by the nightly tick. It keeps the
// lag gauge current and, when Lark is configured, alerts on rows that have
// been waiting for more than 1.5 windows.
func (s *publishsrvc) refreshNightlyMetricsAndAlerts(ctx context.Context) {
	now := s.now()
	rows, err := s.db.PublishDesired.Query().All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to scan nightly rows for lag alerts")
		return
	}
	// One verification pass feeds both the gauge and the alert, so they can
	// never disagree about what "published" means.
	results := s.verifyNightlyRows(ctx, rows)

	if s.metrics != nil {
		s.metrics.refresh(results, now)
		s.metrics.refreshStarvation(ctx, s.db, now)
	}
	if s.alerter == nil || s.nightlyAlerts == nil {
		return
	}
	s.nightlyAlerts.evaluate(ctx, s.alerter, results, now, func(target string) time.Duration {
		return s.publishConfig().Nightly.WindowFor(target).WindowDuration()
	}, s.logger)
}
