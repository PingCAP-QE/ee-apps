package impl

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishdesired "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishdesired"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func boolPtr(v bool) *bool { return &v }

func newDesiredTestStore(t *testing.T) (*desiredStore, *ent.Client) {
	t.Helper()
	db := serverlessTestDB(t)
	store := newDesiredStore(db, config.NightlyPublish{})
	store.nowFn = func() time.Time { return time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC) }
	store.window = 12 * time.Hour
	store.anchor = 2 * time.Hour
	store.loc = time.UTC
	store.jitter = 0
	store.retryAfter = 5 * time.Minute
	store.batch = 10
	return store, db
}

func desiredKeyForTest(target, pkg, osName, arch string) desiredKey {
	return desiredKey{Target: target, Pkg: pkg, OS: osName, Arch: arch}
}

func createDesiredRow(t *testing.T, db *ent.Client, key desiredKey, version string, next time.Time, running string) *ent.PublishDesired {
	t.Helper()
	create := db.PublishDesired.Create().
		SetTarget(key.Target).
		SetPkg(key.Pkg).
		SetOs(key.OS).
		SetArch(key.Arch).
		SetDesiredVersion(version).
		SetDesiredPayload(map[string]any{"version": version}).
		SetDesiredAt(next.Add(-time.Hour)).
		SetNextRunAt(next)
	if running != "" {
		create = create.SetRunningUnitID(running)
	}
	row, err := create.Save(context.Background())
	require.NoError(t, err)
	return row
}

func TestNextRunAtAlignsToAnchorAndTimezone(t *testing.T) {
	store := &desiredStore{window: 12 * time.Hour, anchor: 2 * time.Hour, loc: time.UTC}
	cases := []struct {
		name string
		now  time.Time
		want time.Time
	}{
		{"before anchor", time.Date(2026, 10, 30, 1, 59, 0, 0, time.UTC), time.Date(2026, 10, 30, 2, 0, 0, 0, time.UTC)},
		{"exactly at anchor moves to next window", time.Date(2026, 10, 30, 2, 0, 0, 0, time.UTC), time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC)},
		{"inside window", time.Date(2026, 10, 30, 13, 59, 0, 0, time.UTC), time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC)},
		{"crosses midnight", time.Date(2026, 10, 30, 23, 0, 0, 0, time.UTC), time.Date(2026, 10, 31, 2, 0, 0, 0, time.UTC)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, store.nextRunAt(tc.now, "tidb"))
		})
	}

	// The anchor is interpreted in the configured zone: 02:30 at UTC+8 is
	// 18:30 UTC, so the next boundary is 14:00 UTC+8 = 06:00 UTC.
	shanghai, err := time.LoadLocation("Asia/Shanghai")
	require.NoError(t, err)
	store.loc = shanghai
	require.Equal(t,
		time.Date(2026, 10, 30, 6, 0, 0, 0, time.UTC),
		store.nextRunAt(time.Date(2026, 10, 29, 18, 30, 0, 0, time.UTC), "tidb"))
}

func TestDesiredUpsertKeepsLatestAndLeavesScheduleAlone(t *testing.T) {
	store, db := newDesiredTestStore(t)
	ctx := context.Background()
	key := desiredKeyForTest("tiup-prod", "tidb", "linux", "amd64")
	now := store.now()

	first, err := store.Upsert(ctx, key, "v9.0.0-nightly", map[string]any{"n": 1}, now)
	require.NoError(t, err)
	require.Equal(t, time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC), first.NextRunAt.UTC())

	// An in-flight unit must survive later pushes.
	_, err = db.PublishDesired.Update().
		Where(entpublishdesired.Target(key.Target), entpublishdesired.Pkg(key.Pkg)).
		SetRunningUnitID("unit-running").
		Save(ctx)
	require.NoError(t, err)

	second, err := store.Upsert(ctx, key, "v9.0.1-nightly", map[string]any{"n": 2}, now.Add(time.Minute))
	require.NoError(t, err)
	third, err := store.Upsert(ctx, key, "v9.0.2-nightly", map[string]any{"n": 3}, now.Add(2*time.Minute))
	require.NoError(t, err)

	require.Equal(t, "v9.0.2-nightly", third.DesiredVersion, "the newest push wins")
	require.Equal(t, float64(3), third.DesiredPayload["n"])
	require.Equal(t, first.NextRunAt.UTC(), third.NextRunAt.UTC(), "upsert must not move the schedule")
	require.Equal(t, "unit-running", third.RunningUnitID, "upsert must not disturb the in-flight unit")
	require.Equal(t, second.CreatedAt.UTC(), third.CreatedAt.UTC())

	count, err := db.PublishDesired.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)
}

func TestDesiredClaimTakesOnlyDueIdleRowsForTheWorkerTargets(t *testing.T) {
	store, db := newDesiredTestStore(t)
	ctx := context.Background()
	now := store.now()

	due := createDesiredRow(t, db, desiredKeyForTest("tiup-prod", "tidb", "linux", "amd64"),
		"v9.0.0-nightly", now.Add(-time.Minute), "")
	createDesiredRow(t, db, desiredKeyForTest("tiup-prod", "tikv", "linux", "amd64"),
		"v9.0.0-nightly", now.Add(-time.Minute), "already-running")
	createDesiredRow(t, db, desiredKeyForTest("tiup-prod", "pd", "linux", "amd64"),
		"v9.0.0-nightly", now.Add(time.Hour), "")
	createDesiredRow(t, db, desiredKeyForTest("tiup-staging", "tidb", "linux", "amd64"),
		"v9.0.0-nightly", now.Add(-time.Minute), "")

	claims, err := store.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)
	require.Equal(t, due.ID, claims[0].Row.ID)
	require.NotEmpty(t, claims[0].UnitID)
	require.Equal(t, claims[0].UnitID, claims[0].Row.RunningUnitID)

	// A second worker (a second store over the same database) sees no claim.
	other := *store
	other.newID = func() string { return "other-unit" }
	claims, err = other.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Empty(t, claims, "the conditional update must leave nothing for another worker")

	// The other target is still claimable by its own worker.
	claims, err = store.ClaimDue(ctx, []string{"tiup-staging"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)
	require.Equal(t, "tiup-staging", claims[0].Row.Target)
}

func TestDesiredFailureRetriesShortThenWaitsForTheWindow(t *testing.T) {
	store, db := newDesiredTestStore(t)
	ctx := context.Background()
	key := desiredKeyForTest("tiup-prod", "tidb", "linux", "amd64")
	now := store.now()

	require.NoError(t, db.PublishDesired.Create().
		SetTarget(key.Target).SetPkg(key.Pkg).SetOs(key.OS).SetArch(key.Arch).
		SetDesiredVersion("v9.0.0-nightly").
		SetDesiredPayload(map[string]any{"version": "v9.0.0-nightly"}).
		SetDesiredAt(now).
		SetNextRunAt(now.Add(-time.Minute)).
		Exec(ctx))

	claims, err := store.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)
	require.NoError(t, store.Settle(ctx, claims[0].UnitID, PublishStateFailed, "v9.0.0-nightly", "publish failed", now))

	row, err := store.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.Equal(t, "v9.0.0-nightly", row.DesiredVersion, "a failure keeps the newest desired artifact")
	require.Equal(t, "v9.0.0-nightly", row.LastFailedVersion)
	require.Equal(t, now.Add(5*time.Minute).UTC(), row.NextRunAt.UTC(), "first failure retries after 5m")

	// The retry fails again: the schedule falls back to the next window.
	now = now.Add(5 * time.Minute)
	claims, err = store.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)
	require.NoError(t, store.Settle(ctx, claims[0].UnitID, PublishStateFailed, "v9.0.0-nightly", "publish failed again", now))

	row, err = store.Get(ctx, key)
	require.NoError(t, err)
	require.Equal(t, time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC), row.NextRunAt.UTC(), "later failures wait for the window")
}

func TestDesiredSuccessRecordsVersionAndAdvancesWindow(t *testing.T) {
	store, db := newDesiredTestStore(t)
	ctx := context.Background()
	key := desiredKeyForTest("tiup-prod", "tidb", "linux", "amd64")
	now := store.now()

	createDesiredRow(t, db, key, "v9.0.0-nightly", now.Add(-time.Minute), "")
	claims, err := store.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)

	// A newer push arrives while the unit runs: it replaces desired_version
	// but must not be reported as the published one.
	_, err = store.Upsert(ctx, key, "v9.0.1-nightly", map[string]any{"version": "v9.0.1-nightly"}, now.Add(time.Second))
	require.NoError(t, err)

	require.NoError(t, store.Settle(ctx, claims[0].UnitID, PublishStateSucceeded, "v9.0.0-nightly", "", now.Add(time.Minute)))

	row, err := store.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.Equal(t, "v9.0.1-nightly", row.DesiredVersion)
	require.Equal(t, "v9.0.0-nightly", row.LastSucceededVersion)
	require.Equal(t, time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC), row.NextRunAt.UTC())
}

func TestReconcileNightlyCreatesOneUnitAndSettlesOnCompletion(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	enabled := true
	svc.publishCfg.Nightly = config.NightlyPublish{
		Enabled: &enabled,
		Targets: []string{delivery.TiUPTargetProd},
	}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }
	svc.desired.jitter = 0
	svc.desired.retryAfter = 5 * time.Minute

	key := desiredKeyForTest(delivery.TiUPTargetProd, "tidb", "linux", "amd64")
	payload := TiUPStepPayload{
		Mirror:         delivery.TiUPMirrorProd,
		MirrorURL:      "http://tiup.pingcap.net:8987",
		ArtifactURL:    nightlyArtifact,
		Digest:         "sha256:1111111111111111111111111111111111111111111111111111111111111111",
		Nightly:        true,
		NightlyVersion: "v9.0.0-nightly",
		Packages: []delivery.TiUPPackage{{
			Name: "tidb", Version: "v9.0.0-nightly", OS: "linux", Arch: "amd64",
			EntryPoint: "tidb-server", File: "tidb-v9.0.0-nightly-linux-amd64.tar.gz",
		}},
	}
	raw, err := payloadMap(payload)
	require.NoError(t, err)
	_, err = svc.desired.Upsert(ctx, key, payload.NightlyVersion, raw, *clock)
	require.NoError(t, err)
	// Make the row due now.
	_, err = svc.db.PublishDesired.Update().
		Where(entpublishdesired.Target(key.Target), entpublishdesired.Pkg(key.Pkg)).
		SetNextRunAt(clock.Add(-time.Second)).
		Save(ctx)
	require.NoError(t, err)

	created, err := svc.reconcileNightlyOnce(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, created)

	row, err := svc.desired.Get(ctx, key)
	require.NoError(t, err)
	require.NotEmpty(t, row.RunningUnitID)
	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)

	unit, err := svc.db.PublishUnit.Get(ctx, row.RunningUnitID)
	require.NoError(t, err)
	require.Equal(t, PublishKindTiUP, unit.Kind)
	require.Equal(t, "nightly", unit.RequestedBy)

	step, err := svc.db.PublishStep.Query().Where(publishstep.UnitID(unit.ID)).Only(ctx)
	require.NoError(t, err)
	require.Equal(t, delivery.TiUPTargetProd, step.Target)

	// A second tick must not duplicate the unit while the first is running.
	created, err = svc.reconcileNightlyOnce(ctx)
	require.NoError(t, err)
	require.Zero(t, created)
	count, err = svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)

	// Simulate the worker finishing the step; refreshUnit settles the row.
	_, err = svc.db.PublishStep.Update().
		Where(publishstep.ID(step.ID)).
		SetState(PublishStepStateSucceeded).
		SetFinishedAt(*clock).
		Save(ctx)
	require.NoError(t, err)
	svc.refreshUnit(ctx, unit.ID)

	row, err = svc.desired.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.Equal(t, "v9.0.0-nightly", row.LastSucceededVersion)
	require.Equal(t, time.Date(2026, 10, 30, 14, 0, 0, 0, time.UTC), row.NextRunAt.UTC())
}

func TestDeliveryByRulesDefersOnlyTheConfiguredTarget(t *testing.T) {
	enabled := true
	cfg := config.Publish{Nightly: config.NightlyPublish{
		Enabled: &enabled,
		Targets: []string{delivery.TiUPTargetStaging},
	}}
	svc := newTiUPServiceForTest(t, cfg)
	svc.desired = newDesiredStore(svc.db, cfg.Nightly)
	svc.desired.jitter = 0
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	ids, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, ids, 1, "only the non-deferred prod target creates a step")

	step, err := svc.db.PublishStep.Get(context.Background(), ids[0])
	require.NoError(t, err)
	require.Equal(t, delivery.TiUPTargetProd, step.Target)

	rows, err := svc.db.PublishDesired.Query().All(context.Background())
	require.NoError(t, err)
	require.Len(t, rows, 4, "the tidb artifact carries four packages")
	for _, row := range rows {
		require.Equal(t, delivery.TiUPTargetStaging, row.Target)
		require.NotEmpty(t, row.NextRunAt)
	}
}

func TestDesiredJitterIsStableAndBounded(t *testing.T) {
	store := &desiredStore{jitter: 30 * time.Minute}
	first := store.jitterFor("tidb")
	if first != store.jitterFor("tidb") {
		t.Fatal("jitterFor() is not stable for the same package")
	}
	for _, pkg := range []string{"tidb", "tikv", "pd", "tiflow", "ticdc", "tiflash", "tiproxy", "tidb-binlog", "tidb-dashboard", "monitoring", "tiup", "ctl", "tici"} {
		if got := store.jitterFor(pkg); got < 0 || got >= 30*time.Minute {
			t.Fatalf("jitterFor(%q) = %v, want [0, 30m)", pkg, got)
		}
	}
	store.jitter = 0
	if got := store.jitterFor("tidb"); got != 0 {
		t.Fatalf("jitterFor() = %v with jitter disabled, want 0", got)
	}
}

func TestDesiredReleaseReturnsTheRowToTheSchedule(t *testing.T) {
	store, db := newDesiredTestStore(t)
	ctx := context.Background()
	key := desiredKeyForTest("tiup-prod", "tidb", "linux", "amd64")
	now := store.now()

	createDesiredRow(t, db, key, "v9.0.0-nightly", now.Add(-time.Minute), "")
	claims, err := store.ClaimDue(ctx, []string{"tiup-prod"}, now)
	require.NoError(t, err)
	require.Len(t, claims, 1)

	require.NoError(t, store.Release(ctx, key, claims[0].UnitID, errors.New("unit creation failed"), now))

	row, err := store.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.Equal(t, now.Add(5*time.Minute).UTC(), row.NextRunAt.UTC())
	require.Contains(t, row.LastError, "unit creation failed")
}

func TestNightlySweepReleasesRowsWhoseUnitDisappeared(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	enabled := true
	svc.publishCfg.Nightly = config.NightlyPublish{Enabled: &enabled, Targets: []string{delivery.TiUPTargetProd}}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }
	svc.desired.retryAfter = 5 * time.Minute

	key := desiredKeyForTest(delivery.TiUPTargetProd, "tidb", "linux", "amd64")
	createDesiredRow(t, svc.db, key, "v9.0.0-nightly", clock.Add(-time.Minute), "ghost-unit")

	svc.settleNightlyClaims(ctx)

	row, err := svc.desired.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.Contains(t, row.LastError, "disappeared")
	require.Equal(t, clock.Add(5*time.Minute).UTC(), row.NextRunAt.UTC())
}

func TestReconcileNightlyReleasesMalformedDesiredRows(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	enabled := true
	svc.publishCfg.Nightly = config.NightlyPublish{Enabled: &enabled, Targets: []string{delivery.TiUPTargetProd}}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }

	key := desiredKeyForTest(delivery.TiUPTargetProd, "tidb", "linux", "amd64")
	createDesiredRow(t, svc.db, key, "v9.0.0-nightly", clock.Add(-time.Minute), "")

	created, err := svc.reconcileNightlyOnce(ctx)
	require.NoError(t, err)
	require.Zero(t, created)

	row, err := svc.desired.Get(ctx, key)
	require.NoError(t, err)
	require.Empty(t, row.RunningUnitID)
	require.NotEmpty(t, row.LastError)
}

func TestNightlyReconcilerRunsOnItsTick(t *testing.T) {
	svc, clock := newFlowTestService(t)
	enabled := true
	svc.publishCfg.Nightly = config.NightlyPublish{
		Enabled:      &enabled,
		Targets:      []string{delivery.TiUPTargetProd},
		TickInterval: "5ms",
	}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }
	svc.desired.jitter = 0

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
	_, err = svc.desired.Upsert(context.Background(), key, payload.NightlyVersion, raw, *clock)
	require.NoError(t, err)
	_, err = svc.db.PublishDesired.Update().
		Where(entpublishdesired.Target(key.Target), entpublishdesired.Pkg(key.Pkg)).
		SetNextRunAt(clock.Add(-time.Second)).
		Save(context.Background())
	require.NoError(t, err)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go svc.runNightlyReconciler(ctx)

	require.Eventually(t, func() bool {
		count, err := svc.db.PublishUnit.Query().Count(ctx)
		return err == nil && count == 1
	}, 2*time.Second, 20*time.Millisecond)
}
