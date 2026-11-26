package relationships

import (
	"fmt"
	"strings"
	"sync/atomic"
	"time"
)

// Metrics is the Reporter the sync job exposes. It keeps the outcome counters
// and the observation lag of the last successful reconcile, which is how
// operators see revocation delay.
type Metrics struct {
	now func() time.Time

	writes      atomic.Uint64
	deletes     atomic.Uint64
	lastLag     atomic.Int64
	lastSuccess atomic.Int64

	outcomes map[string]*atomic.Uint64
}

// NewMetrics returns a zeroed recorder.
func NewMetrics() *Metrics {
	return &Metrics{
		now: time.Now,
		outcomes: map[string]*atomic.Uint64{
			"applied":                {},
			"noop":                   {},
			"error":                  {},
			"incomplete_snapshot":    {},
			"empty_snapshot_refused": {},
			"invalid_snapshot":       {},
			"invalid_tuple":          {},
			"out_of_scope_tuple":     {},
			"planned":                {},
		},
	}
}

// WithClock overrides the clock used for the success timestamp.
func (m *Metrics) WithClock(now func() time.Time) *Metrics {
	if now != nil {
		m.now = now
	}
	return m
}

// Report implements Reporter.
func (m *Metrics) Report(result Result, err error) {
	if m == nil {
		return
	}
	outcome := Outcome(result, err)
	if counter, ok := m.outcomes[outcome]; ok {
		counter.Add(1)
	} else {
		m.outcomes["error"].Add(1)
	}
	if err != nil {
		return
	}
	m.writes.Add(uint64(result.Writes))
	m.deletes.Add(uint64(result.Deletes))
	m.lastLag.Store(int64(result.Lag / time.Second))
	m.lastSuccess.Store(m.now().Unix())
}

// Render returns the Prometheus text exposition for the job.
func (m *Metrics) Render() string {
	if m == nil {
		return ""
	}
	var out strings.Builder
	out.WriteString("# TYPE authzd_tuple_sync_total counter\n")
	for _, outcome := range sortedOutcomes() {
		fmt.Fprintf(&out, "authzd_tuple_sync_total{outcome=%q} %d\n", outcome, m.outcomes[outcome].Load())
	}
	out.WriteString("# TYPE authzd_tuple_sync_last_success_timestamp_seconds gauge\n")
	fmt.Fprintf(&out, "authzd_tuple_sync_last_success_timestamp_seconds %g\n", float64(m.lastSuccess.Load()))
	out.WriteString("# TYPE authzd_tuple_sync_lag_seconds gauge\n")
	fmt.Fprintf(&out, "authzd_tuple_sync_lag_seconds %d\n", m.lastLag.Load())
	out.WriteString("# TYPE authzd_tuple_sync_writes_total counter\n")
	fmt.Fprintf(&out, "authzd_tuple_sync_writes_total %d\n", m.writes.Load())
	out.WriteString("# TYPE authzd_tuple_sync_deletes_total counter\n")
	fmt.Fprintf(&out, "authzd_tuple_sync_deletes_total %d\n", m.deletes.Load())
	return out.String()
}

func sortedOutcomes() []string {
	return []string{
		"applied", "empty_snapshot_refused", "error", "incomplete_snapshot",
		"invalid_snapshot", "invalid_tuple", "noop", "out_of_scope_tuple", "planned",
	}
}
