package impl

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func stepByID(t *testing.T, svc *publishsrvc, id string) *ent.PublishStep {
	t.Helper()
	step, err := svc.db.PublishStep.Get(context.Background(), id)
	require.NoError(t, err)
	return step
}

func unitByID(t *testing.T, svc *publishsrvc, id string) *ent.PublishUnit {
	t.Helper()
	unit, err := svc.db.PublishUnit.Get(context.Background(), id)
	require.NoError(t, err)
	return unit
}

// A worker killed mid-step leaves the step claimed with an expired lease; the
// reaper must make it claimable again instead of wedging the unit forever.
func TestReaperRequeuesAbandonedStep(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	svc.workerCfg.MaxAttempts = 3

	unit, prod, _ := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, prod.ID, claimed.ID)

	// The worker dies: no finish, the lease simply runs out.
	*clock = clock.Add(svc.workerCfg.LeaseDuration() + time.Minute)

	reaped, err := svc.reapExpiredSteps(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, reaped)

	requeued := stepByID(t, svc, prod.ID)
	require.Equal(t, PublishStepStatePending, requeued.State)
	require.Empty(t, requeued.ClaimedBy)
	require.NotNil(t, requeued.AvailableAt, "a requeued step backs off before the next attempt")
	require.Equal(t, PublishStateQueued, unitByID(t, svc, unit.ID).State)

	// After the backoff another worker can take it over.
	*clock = clock.Add(2 * publishBackoffMax)
	taken, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, taken)
	require.Equal(t, prod.ID, taken.ID)
	require.Equal(t, 2, taken.Attempts)
}

// When the attempts are exhausted the step fails, its dependents are skipped
// and the unit converges to a terminal state instead of staying claimed.
func TestReaperFailsStepAfterMaxAttempts(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()
	svc.workerCfg.MaxAttempts = 1

	unit, prod, staging := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	_, err := svc.claimStep(ctx)
	require.NoError(t, err)
	*clock = clock.Add(svc.workerCfg.LeaseDuration() + time.Minute)

	reaped, err := svc.reapExpiredSteps(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, reaped)

	failed := stepByID(t, svc, prod.ID)
	require.Equal(t, PublishStepStateFailed, failed.State)
	require.Contains(t, failed.LastError, "lease expired")

	require.Equal(t, PublishStepStateSkipped, stepByID(t, svc, staging.ID).State)

	stored := unitByID(t, svc, unit.ID)
	require.Equal(t, PublishStateFailed, stored.State)
	require.NotNil(t, stored.FinishedAt)
}

// A dependent step is never claimable while its predecessor is unfinished.
func TestDependentStepIsNotClaimable(t *testing.T) {
	svc, _ := newFlowTestService(t)
	ctx := context.Background()

	_, prod, _ := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, prod.ID, claimed.ID)

	// The staging step is blocked, so nothing else is claimable.
	claimed, err = svc.claimStep(ctx)
	require.NoError(t, err)
	require.Nil(t, claimed)
}

// Succeeding a step promotes its dependent and keeps the unit running.
func TestSucceededStepPromotesDependent(t *testing.T) {
	svc, _ := newFlowTestService(t)
	ctx := context.Background()

	unit, prod, staging := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)

	ok, err := svc.markStepSucceeded(ctx, claimed, map[string]any{"remote_sha256": "abc"})
	require.NoError(t, err)
	require.True(t, ok)
	svc.refreshUnit(ctx, unit.ID)

	require.Equal(t, PublishStepStateSucceeded, stepByID(t, svc, prod.ID).State)
	require.Equal(t, PublishStepStatePending, stepByID(t, svc, staging.ID).State)
	require.Equal(t, PublishStateQueued, unitByID(t, svc, unit.ID).State)
}

// A failed step with no successful sibling fails the unit and skips its
// dependents.
func TestFailedStepSkipsDependents(t *testing.T) {
	svc, _ := newFlowTestService(t)
	ctx := context.Background()
	svc.workerCfg.MaxAttempts = 1

	unit, prod, staging := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)

	ok, err := svc.handleStepFailure(ctx, claimed, context.DeadlineExceeded)
	require.NoError(t, err)
	require.True(t, ok)
	svc.refreshUnit(ctx, unit.ID)

	require.Equal(t, PublishStepStateFailed, stepByID(t, svc, prod.ID).State)
	require.Equal(t, PublishStepStateSkipped, stepByID(t, svc, staging.ID).State)
	require.Equal(t, PublishStateFailed, unitByID(t, svc, unit.ID).State)
}

// A partial mirror state after a crash is PARTIAL, and resume re-runs only the
// failed step.
func TestPartialThenResume(t *testing.T) {
	svc, _ := newFlowTestService(t)
	ctx := context.Background()
	svc.workerCfg.MaxAttempts = 1

	unit, prod, staging := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	// prod succeeds.
	first, err := svc.claimStep(ctx)
	require.NoError(t, err)
	ok, err := svc.markStepSucceeded(ctx, first, nil)
	require.NoError(t, err)
	require.True(t, ok)
	svc.refreshUnit(ctx, unit.ID)

	// staging fails.
	second, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, second)
	ok, err = svc.handleStepFailure(ctx, second, context.DeadlineExceeded)
	require.NoError(t, err)
	require.True(t, ok)
	svc.refreshUnit(ctx, unit.ID)

	require.Equal(t, PublishStatePartial, unitByID(t, svc, unit.ID).State)

	resumed, err := svc.RetryUnit(ctx, &publish.RetryUnitPayload{ID: unit.ID})
	require.NoError(t, err)
	require.Equal(t, publish.PublishState("queued"), resumed.State)
	require.Equal(t, PublishStepStateSucceeded, stepByID(t, svc, prod.ID).State)
	require.Equal(t, PublishStepStatePending, stepByID(t, svc, staging.ID).State)
}

// A conflicting remote package fails the step and the unit, and the error says
// why: this is the "never overwrite" invariant under fault injection.
func TestExecuteStepFailsUnitOnShaMismatch(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{tiupTestPackage.File: []byte("different")}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	unit, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})
	svc.workerCfg.MaxAttempts = 1

	claimed, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, step.ID, claimed.ID)
	svc.executeStep(context.Background(), claimed)

	failed := stepByID(t, svc, step.ID)
	require.Equal(t, PublishStepStateFailed, failed.State)
	require.Contains(t, failed.LastError, "refusing to overwrite")

	stored := unitByID(t, svc, unit.ID)
	require.Equal(t, PublishStateFailed, stored.State)
	require.NotNil(t, stored.FinishedAt)
}

// Reaping runs from the claim loop, so a crashed worker is recovered without
// an operator.
func TestReapIsCalledByTheWorkerLoop(t *testing.T) {
	svc, clock := newFlowTestService(t)
	ctx := context.Background()

	_, prod, _ := createDependentUnit(t, svc, "tiup-prod", "tiup-staging")

	claimed, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)

	*clock = clock.Add(svc.workerCfg.LeaseDuration() + publishBackoffMax)

	// drainOnce runs the reap + claim path the runner uses.
	svc.consumePublishSteps(ctx)

	require.Equal(t, PublishStepStateClaimed, stepByID(t, svc, prod.ID).State)
	require.NotEmpty(t, stepByID(t, svc, prod.ID).ClaimedBy)
}

var _ = entpublishstep.FieldState
