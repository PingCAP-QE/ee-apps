package impl

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishlane "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishlane"
)

// newLaneTestFixture returns a lane bound to an in-memory database and a clock
// the test controls.
func newLaneTestFixture(t *testing.T) (*publishLane, *time.Time) {
	t.Helper()
	db := serverlessTestDB(t)
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	clock := &now
	lane := newPublishLane(db, PublishLaneTiUP)
	lane.nowFn = func() time.Time { return *clock }
	require.NoError(t, lane.ensure(context.Background()))
	return lane, clock
}

func laneRow(t *testing.T, lane *publishLane) *ent.PublishLane {
	t.Helper()
	row, err := lane.db.PublishLane.Query().
		Where(entpublishlane.Lane(PublishLaneTiUP)).
		Only(context.Background())
	require.NoError(t, err)
	return row
}

// The semaphore is a single row: seeding twice must not create a second one,
// otherwise two units could hold "the" lane at the same time.
func TestLaneEnsureIsIdempotentAndUnique(t *testing.T) {
	lane, _ := newLaneTestFixture(t)
	ctx := context.Background()

	require.NoError(t, lane.ensure(ctx))
	require.NoError(t, lane.ensure(ctx))

	count, err := lane.db.PublishLane.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)

	row := laneRow(t, lane)
	require.Equal(t, 1, row.ID)
	require.Equal(t, PublishLaneTiUP, row.Lane)
	require.Empty(t, row.UnitID)
	require.Nil(t, row.LeaseUntil)
}

// Only one unit may hold the lane; a second unit waits until release.
func TestLaneAcquireExcludesSecondUnit(t *testing.T) {
	lane, _ := newLaneTestFixture(t)
	ctx := context.Background()

	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", 15*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	ok, err = lane.Acquire(ctx, "unit-b", "worker-2", 15*time.Minute)
	require.NoError(t, err)
	require.False(t, ok, "the lane is held by unit-a")

	row := laneRow(t, lane)
	require.Equal(t, "unit-a", row.UnitID)
	require.Equal(t, "worker-1", row.Holder)

	require.NoError(t, lane.Release(ctx, "unit-b"))
	row = laneRow(t, lane)
	require.Equal(t, "unit-a", row.UnitID, "a non-holder must not release the lane")

	require.NoError(t, lane.Release(ctx, "unit-a"))
	ok, err = lane.Acquire(ctx, "unit-b", "worker-2", 15*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	row = laneRow(t, lane)
	require.Equal(t, "unit-b", row.UnitID)
}

// An expired lease is reclaimable: that is how a unit survives a dead worker.
func TestLaneExpiredLeaseIsReclaimable(t *testing.T) {
	lane, clock := newLaneTestFixture(t)
	ctx := context.Background()

	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", 10*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	*clock = clock.Add(10*time.Minute + time.Second)

	ok, err = lane.Acquire(ctx, "unit-b", "worker-2", 10*time.Minute)
	require.NoError(t, err)
	require.True(t, ok, "an expired lease must be reclaimable")

	row := laneRow(t, lane)
	require.Equal(t, "unit-b", row.UnitID)
	require.Equal(t, "worker-2", row.Holder)

	// The old owner must not be able to renew or validate after the takeover.
	ok, err = lane.Renew(ctx, "unit-a", "worker-1", 10*time.Minute)
	require.NoError(t, err)
	require.False(t, ok)
	ok, err = lane.Validate(ctx, "unit-a", "worker-1")
	require.NoError(t, err)
	require.False(t, ok)
}

// Renew and Validate are fenced on both unit and holder: a stale worker with a
// matching unit id but a superseded holder must not act.
func TestLaneRenewAndValidateAreFenced(t *testing.T) {
	lane, clock := newLaneTestFixture(t)
	ctx := context.Background()

	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", 10*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	ok, err = lane.Renew(ctx, "unit-b", "worker-2", 10*time.Minute)
	require.NoError(t, err)
	require.False(t, ok, "another unit must not renew")

	ok, err = lane.Renew(ctx, "unit-a", "worker-2", 10*time.Minute)
	require.NoError(t, err)
	require.False(t, ok, "another worker must not renew for the same unit")

	ok, err = lane.Renew(ctx, "unit-a", "worker-1", 20*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	ok, err = lane.Validate(ctx, "unit-a", "worker-1")
	require.NoError(t, err)
	require.True(t, ok)

	// Validate also fails once the lease lapsed, even without a takeover.
	*clock = clock.Add(21 * time.Minute)
	ok, err = lane.Validate(ctx, "unit-a", "worker-1")
	require.NoError(t, err)
	require.False(t, ok)
}

// A failed heartbeat means the lease was lost: the running copy must be
// cancelled so no further irreversible action is taken.
func TestLaneHeartbeatCancelsWhenLeaseIsLost(t *testing.T) {
	db := serverlessTestDB(t)
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	clock := &now
	lane := newPublishLane(db, PublishLaneTiUP)
	lane.nowFn = func() time.Time { return *clock }
	ctx := context.Background()
	require.NoError(t, lane.ensure(ctx))

	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", time.Second)
	require.NoError(t, err)
	require.True(t, ok)

	run, cancel := context.WithCancel(ctx)
	defer cancel()

	// Take over the lane: the heartbeat below must observe the loss.
	*clock = clock.Add(2 * time.Second)
	ok, err = lane.Acquire(ctx, "unit-b", "worker-2", time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	lane.heartbeat(run, cancel, "unit-a", "worker-1", 10*time.Millisecond, time.Second)

	select {
	case <-run.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("heartbeat did not cancel the execution after losing the lane")
	}
	require.ErrorIs(t, run.Err(), context.Canceled)
}

// The heartbeat stops when the caller is done, leaving no goroutine behind.
func TestLaneHeartbeatStopsWithContext(t *testing.T) {
	lane, _ := newLaneTestFixture(t)
	ctx := context.Background()
	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	run, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		lane.heartbeat(run, cancel, "unit-a", "worker-1", time.Hour, time.Minute)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("heartbeat did not stop after the context was cancelled")
	}
}

// Repeated auto-migration keeps the seeded lane row and its state.
func TestLaneSurvivesRepeatedSchemaCreate(t *testing.T) {
	lane, _ := newLaneTestFixture(t)
	ctx := context.Background()

	ok, err := lane.Acquire(ctx, "unit-a", "worker-1", time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	require.NoError(t, lane.db.Schema.Create(ctx))
	require.NoError(t, lane.db.Schema.Create(ctx))

	count, err := lane.db.PublishLane.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)

	row := laneRow(t, lane)
	require.Equal(t, "unit-a", row.UnitID)
}

func TestTerminalUnitReleasesLane(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	unit, step := createTiUPStep(t, svc, "tiup-staging", TiUPStepPayload{
		Mirror: "staging", MirrorURL: "http://127.0.0.1:1",
	})

	ok, err := svc.lane.Acquire(ctx, unit.ID, "worker-1", 12*time.Minute)
	require.NoError(t, err)
	require.True(t, ok)

	_, err = svc.db.PublishStep.UpdateOneID(step.ID).
		SetState(PublishStepStateSucceeded).
		SetFinishedAt(*clock).
		Save(ctx)
	require.NoError(t, err)

	svc.refreshUnit(ctx, unit.ID)

	ok, err = svc.lane.Acquire(ctx, "other-unit", "worker-2", time.Minute)
	require.NoError(t, err)
	require.True(t, ok, "a terminal unit must release the lane immediately")
}

func TestLaneBusyDeferDoesNotConsumeAttempts(t *testing.T) {
	svc, _ := newFlowTestService(t)
	ctx := context.Background()
	_, step := createTiUPStep(t, svc, "tiup-staging", TiUPStepPayload{
		Mirror: "staging", MirrorURL: "http://127.0.0.1:1",
	})

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, 1, claimed.Attempts)

	ok, err := svc.deferStep(ctx, claimed)
	require.NoError(t, err)
	require.True(t, ok)

	stored, err := svc.db.PublishStep.Get(ctx, step.ID)
	require.NoError(t, err)
	require.Equal(t, 0, stored.Attempts, "waiting for the lane is not an execution attempt")
	require.Equal(t, PublishStepStatePending, stored.State)
	require.NotNil(t, stored.AvailableAt)
	require.GreaterOrEqual(t, stored.AvailableAt.Sub(svc.now()), 24*time.Second)
}

// A lane that stays busy for a long time must not exhaust the step's execution
// budget: waiting is not an attempt, however many times it is requeued.
func TestLaneBusyRequeueKeepsAttemptsAcrossManyWaits(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	_, step := createTiUPStep(t, svc, "tiup-staging", TiUPStepPayload{
		Mirror: "staging", MirrorURL: "http://127.0.0.1:1",
	})
	svc.workerCfg.MaxAttempts = 3

	for i := 0; i < 100; i++ {
		claimed, err := svc.claimStep(ctx)
		require.NoError(t, err)
		require.NotNil(t, claimed, "wait %d: the step must stay claimable", i)
		require.Equal(t, 1, claimed.Attempts, "wait %d: the claim itself is the only attempt", i)

		ok, err := svc.deferStep(ctx, claimed)
		require.NoError(t, err)
		require.True(t, ok, "wait %d: the step must be requeued", i)

		stored, err := svc.db.PublishStep.Get(ctx, step.ID)
		require.NoError(t, err)
		require.Equal(t, 0, stored.Attempts, "wait %d: waiting must not consume an attempt", i)
		require.Equal(t, PublishStepStatePending, stored.State)

		*clock = clock.Add(40 * time.Second)
	}
}
