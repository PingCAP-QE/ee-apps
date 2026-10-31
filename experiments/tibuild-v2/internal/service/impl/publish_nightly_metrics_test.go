package impl

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	entpublishdesired "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishdesired"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func scrapeNightlyMetrics(t *testing.T, svc *publishsrvc) string {
	t.Helper()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "/metrics", nil)
	handler := svc.MetricsHandler()
	require.NotNil(t, handler)
	handler.ServeHTTP(recorder, request)
	return recorder.Body.String()
}

func TestNightlyMetricsExposeLagAndCounters(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	svc.metrics = newNightlyMetrics()
	svc.nightlyAlerts = newNightlyAlertTracker(time.Hour)
	notifier := &fakeAlertNotifier{}
	svc.alerter = notifier

	enabled := true
	svc.publishCfg.Nightly = nightlyTestConfig(enabled, delivery.TiUPTargetProd)
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }

	key := desiredKeyForTest(delivery.TiUPTargetProd, "tidb", "linux", "amd64")
	createDesiredRow(t, svc.db, key, "v9.0.0-nightly", clock.Add(-time.Hour), "")
	_, err := svc.db.PublishDesired.Update().
		Where(entpublishdesired.Target(key.Target), entpublishdesired.Pkg(key.Pkg)).
		SetDesiredAt(clock.Add(-19 * time.Hour)).
		Save(ctx)
	require.NoError(t, err)

	svc.refreshNightlyMetricsAndAlerts(ctx)
	body := scrapeNightlyMetrics(t, svc)
	require.Contains(t, body, `publish_desired_lag_seconds{target="tiup-prod"} 68400`)
	require.Equal(t, 1, notifier.count(), "a row older than 1.5 windows alerts once")

	svc.refreshNightlyMetricsAndAlerts(ctx)
	require.Equal(t, 1, notifier.count(), "the same row is deduplicated")

	svc.metrics.recordDue(delivery.TiUPTargetProd)
	svc.metrics.recordPublished(PublishStateSucceeded)
	body = scrapeNightlyMetrics(t, svc)
	require.Contains(t, body, `publish_desired_due_total{target="tiup-prod"} 1`)
	require.Contains(t, body, `nightly_publish_total{result="succeeded"} 1`)
}
