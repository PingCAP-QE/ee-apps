package relationships

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestMetricsCountOutcomesAndKeepLastSuccess(t *testing.T) {
	metrics := NewMetrics()
	metrics.WithClock(func() time.Time { return fixedNow })
	metrics.Report(Result{}, nil)
	metrics.Report(Result{Applied: true, Writes: 3, Deletes: 1, Lag: 90 * time.Second}, nil)
	metrics.Report(Result{}, ErrIncompleteSnapshot)
	metrics.Report(Result{}, errors.New("openfga down"))

	rendered := metrics.Render()
	for _, want := range []string{
		`authzd_tuple_sync_total{outcome="applied"} 1`,
		`authzd_tuple_sync_total{outcome="noop"} 1`,
		`authzd_tuple_sync_total{outcome="incomplete_snapshot"} 1`,
		`authzd_tuple_sync_total{outcome="error"} 1`,
		fmt.Sprintf("authzd_tuple_sync_last_success_timestamp_seconds %g", float64(fixedNow.Unix())),
		`authzd_tuple_sync_lag_seconds 90`,
		`authzd_tuple_sync_writes_total 3`,
		`authzd_tuple_sync_deletes_total 1`,
	} {
		if !strings.Contains(rendered, want) {
			t.Fatalf("Render() missing %q:\n%s", want, rendered)
		}
	}
}

func TestMetricsRenderWithoutSuccess(t *testing.T) {
	metrics := NewMetrics()
	metrics.WithClock(func() time.Time { return fixedNow })
	metrics.Report(Result{}, ErrEmptySnapshot)

	rendered := metrics.Render()
	if !strings.Contains(rendered, `authzd_tuple_sync_last_success_timestamp_seconds 0`) {
		t.Fatalf("Render() without success:\n%s", rendered)
	}
	if !strings.Contains(rendered, `authzd_tuple_sync_lag_seconds 0`) {
		t.Fatalf("Render() reported lag without a successful sync:\n%s", rendered)
	}
}

func TestMetricsRecordsLagOnlyFromSuccessfulSyncs(t *testing.T) {
	metrics := NewMetrics()
	metrics.WithClock(func() time.Time { return fixedNow })
	metrics.Report(Result{Applied: true, Lag: 10 * time.Second}, nil)
	metrics.Report(Result{Lag: 5000 * time.Second}, error(errors.New("boom")))

	if got := metrics.lastLag.Load(); got != 10 {
		t.Fatalf("lag = %d, want the last successful lag 10", got)
	}
}
