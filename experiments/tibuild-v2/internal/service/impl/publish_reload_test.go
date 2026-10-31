package impl

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishdesired "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishdesired"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// TestReloadAppliesNightlySchedule proves the API can change the nightly window
// and target list without a restart: the next tenant read sees the new schedule.
func TestReloadAppliesNightlySchedule(t *testing.T) {
	svc, _ := newFlowTestService(t)

	enabled := true
	svc.Reload(&config.Service{
		Worker: config.Worker{Targets: []string{delivery.TiUPTargetStaging}},
		Publish: config.Publish{
			Nightly: config.NightlyPublish{
				Enabled: &enabled,
				Default: config.NightlyWindow{Window: "12h", Anchor: "02:00"},
				Targets: map[string]config.NightlyWindow{
					delivery.TiUPTargetStaging: {Window: "2h", Anchor: "01:00"},
				},
			},
		},
	})

	window := svc.publishConfig().Nightly.WindowFor(delivery.TiUPTargetStaging)
	require.Equal(t, 2*time.Hour, window.WindowDuration())
	require.Equal(t, "01:00", window.Anchor)
	require.Equal(t, []string{delivery.TiUPTargetStaging}, svc.nightlyTargets())
	require.True(t, svc.nightlyEnabled())
}

// TestReloadChangesNightlyRouting is the behavioural acceptance check: with
// nightly convergence off, a master push queues prod and staging immediately;
// after a reload that enables convergence for staging only, the same push is
// upserted for staging and still queued for prod, without a restart.
func TestReloadChangesNightlyRouting(t *testing.T) {
	rules := goldenRulesLoader(t)
	svc := newTiUPServiceForTest(t, config.Publish{DeliveryConfigPath: rules.path})
	svc.planner = &publishPlanner{rules: rules}
	ctx := context.Background()

	before, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, before, 2, "both mirrors publish immediately while convergence is off")

	enabled := true
	svc.Reload(&config.Service{
		Worker: config.Worker{Targets: []string{delivery.TiUPTargetProd, delivery.TiUPTargetStaging}},
		Publish: config.Publish{
			DeliveryConfigPath: rules.path,
			Nightly: config.NightlyPublish{
				Enabled: &enabled,
				Default: config.NightlyWindow{Window: "12h", Anchor: "02:00"},
				Targets: map[string]config.NightlyWindow{
					delivery.TiUPTargetStaging: {Window: "1h", Anchor: "00:00"},
				},
			},
		},
	})

	after, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	if err != nil {
		t.Fatalf("DeliveryByRules after reload: %#v", err)
	}
	require.Len(t, after, 1, "staging converges now, prod still publishes immediately")

	rows, err := svc.db.PublishDesired.Query().All(ctx)
	require.NoError(t, err)
	require.NotEmpty(t, rows)
	for _, row := range rows {
		require.Equal(t, delivery.TiUPTargetStaging, row.Target)
	}
}

// TestReloadUpdatesWorkerThresholds covers the worker knobs the reload contract
// marks hot: metrics/reap intervals, backoff and alert thresholds.
func TestReloadUpdatesWorkerThresholds(t *testing.T) {
	svc, _ := newFlowTestService(t)

	svc.Reload(&config.Service{Worker: config.Worker{
		MetricsInterval: "45s",
		ReapInterval:    "90s",
		BackoffBase:     "3s",
		BackoffMax:      "1m",
		Alerts:          config.WorkerAlerts{BacklogAfter: "2m", OldestPendingAfter: "3m"},
	}})

	got := svc.workerConfig()
	require.Equal(t, 45*time.Second, got.MetricsEvery())
	require.Equal(t, 90*time.Second, got.ReapEvery())
	require.Equal(t, 3*time.Second, got.BackoffBaseDuration())
	require.Equal(t, time.Minute, got.BackoffMaxDuration())
	require.Equal(t, 2*time.Minute, got.AlertBacklogAfter())
	require.Equal(t, 3*time.Minute, got.AlertOldestPendingAfter())
}

// TestReloadMovesTheDeliveryRulesPath proves publish.delivery_config_path is hot:
// after a reload the planner reads the new file, not the startup one.
func TestReloadMovesTheDeliveryRulesPath(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{})
	first := goldenRulesLoader(t)
	svc.planner = &publishPlanner{rules: first}

	content, err := os.ReadFile(first.path)
	require.NoError(t, err)
	firstPath := first.path
	second := filepath.Join(t.TempDir(), "delivery.yaml")
	require.NoError(t, os.WriteFile(second, content, 0o600))

	svc.Reload(&config.Service{Publish: config.Publish{DeliveryConfigPath: second}})

	rules, err := svc.planner.rules.config()
	require.NoError(t, err)
	require.NotNil(t, rules)

	// Removing the startup file proves the planner no longer reads it.
	require.NoError(t, os.Remove(firstPath))
	_, err = svc.planner.rules.config()
	require.NoError(t, err, "the planner must read the reloaded path")
}

// TestNightlyReconcilerPicksUpTickIntervalChange covers the worker side: the
// reconciler waits one hour, a reload shortens the tick, and the next unit is
// created without a restart.
func TestNightlyReconcilerPicksUpTickIntervalChange(t *testing.T) {
	svc, clock := newFlowTestService(t)
	enabled := true

	initial := nightlyTestConfig(enabled, delivery.TiUPTargetProd)
	initial.TickInterval = "1h"
	svc.publishCfg = config.Publish{Nightly: initial}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, initial)
	svc.desired.nowFn = func() time.Time { return *clock }

	upsertDueDesiredRow(t, svc, *clock)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go svc.runNightlyReconciler(ctx)

	time.Sleep(150 * time.Millisecond)
	require.Zero(t, countPublishUnits(t, svc.db), "the one-hour interval must not tick yet")

	next := initial
	next.TickInterval = "5ms"
	svc.Reload(&config.Service{
		Worker:  config.Worker{Targets: []string{delivery.TiUPTargetProd}},
		Publish: config.Publish{Nightly: next},
	})

	require.Eventually(t, func() bool {
		return countPublishUnits(t, svc.db) == 1
	}, 2*time.Second, 20*time.Millisecond, "the reconciler must follow the reloaded tick interval")
}

// TestArtifactReloadUpdatesWorkerThresholds covers the image sync executor, which
// shares worker.metrics_interval and worker.reap_interval with the publish worker.
func TestArtifactReloadUpdatesWorkerThresholds(t *testing.T) {
	logger := zerolog.Nop()
	svc := &artifactsrvc{logger: &logger}
	svc.applyConfig(&config.Service{Worker: config.Worker{
		MetricsInterval: "20s",
		ReapInterval:    "40s",
	}})

	svc.Reload(&config.Service{Worker: config.Worker{
		MetricsInterval: "25s",
		ReapInterval:    "50s",
	}})

	require.Equal(t, 25*time.Second, svc.workerConfig().MetricsEvery())
	require.Equal(t, 50*time.Second, svc.workerConfig().ReapEvery())
}

// upsertDueDesiredRow records one due desired-state row for the prod mirror.
func upsertDueDesiredRow(t *testing.T, svc *publishsrvc, now time.Time) {
	t.Helper()
	key := desiredKeyForTest(delivery.TiUPTargetProd, "tidb", "linux", "amd64")
	payload := TiUPStepPayload{
		Mirror:         delivery.TiUPMirrorProd,
		MirrorURL:      "http://tiup.pingcap.net:8987",
		ArtifactURL:    nightlyArtifact,
		Nightly:        true,
		NightlyVersion: "v9.0.0-nightly",
		Packages: []delivery.TiUPPackage{{
			Name: "tidb", Version: "v9.0.0-nightly", OS: "linux", Arch: "amd64",
			EntryPoint: "tidb-server", File: "tidb-v9.0.0-nightly-linux-amd64.tar.gz",
		}},
	}
	raw, err := payloadMap(payload)
	require.NoError(t, err)
	_, err = svc.desired.Upsert(context.Background(), key, payload.NightlyVersion, raw, now)
	require.NoError(t, err)
	_, err = svc.db.PublishDesired.Update().
		Where(entpublishdesired.Target(key.Target), entpublishdesired.Pkg(key.Pkg)).
		SetNextRunAt(now.Add(-time.Second)).
		Save(context.Background())
	require.NoError(t, err)
}

func countPublishUnits(t *testing.T, db *ent.Client) int {
	t.Helper()
	count, err := db.PublishUnit.Query().Count(context.Background())
	require.NoError(t, err)
	return count
}
