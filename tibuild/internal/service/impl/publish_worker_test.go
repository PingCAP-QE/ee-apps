package impl

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// registryStub is a fake registry: digests are table-driven and a copy records
// the destination digest, which is what the post-copy verification reads back.
type registryStub struct {
	mu        sync.Mutex
	digests   map[string]string
	resolved  []string
	digestErr error

	copies  []string
	copyErr error
}

func newRegistryStub(digests map[string]string) *registryStub {
	if digests == nil {
		digests = map[string]string{}
	}
	return &registryStub{digests: digests}
}

func (r *registryStub) Digest(_ context.Context, ref string) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.resolved = append(r.resolved, ref)
	if r.digestErr != nil {
		return "", r.digestErr
	}
	digest, ok := r.digests[ref]
	if !ok {
		return "", fmt.Errorf("%s: not found", ref)
	}
	return digest, nil
}

func (r *registryStub) Copy(_ context.Context, src, dst string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.copies = append(r.copies, src+" -> "+dst)
	if r.copyErr != nil {
		return r.copyErr
	}
	at := strings.LastIndex(src, "@")
	if at < 0 {
		return fmt.Errorf("source %q is not pinned to a digest", src)
	}
	r.digests[dst] = src[at+1:]
	return nil
}

func (r *registryStub) copyList() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.copies...)
}

func newPublishWorkerTestService(t *testing.T, ops *registryStub, workerCfg map[string]string) *publishsrvc {
	t.Helper()
	svc := newPublishTestService(t, publishTestRules, nil)
	svc.images = ops
	svc.planner.resolver = ops
	for key, value := range workerCfg {
		switch key {
		case "max_attempts":
			svc.workerCfg.MaxAttempts = atoiOr(t, value)
		case "targets":
			svc.workerCfg.Targets = strings.Split(value, ",")
		case "id":
			svc.workerCfg.ID = value
		}
	}
	return svc
}

func atoiOr(t *testing.T, value string) int {
	t.Helper()
	var out int
	_, err := fmt.Sscanf(value, "%d", &out)
	require.NoError(t, err)
	return out
}

// enqueue plans one image delivery and returns the unit.
func enqueue(t *testing.T, svc *publishsrvc, src string) *publish.PublishUnit {
	t.Helper()
	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr(src),
	})
	require.NoError(t, err)
	require.NotEmpty(t, unit.Steps)
	return unit
}

func TestClaimStepOnlyClaimsConfiguredTargets(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, map[string]string{"targets": "docker.io"})
	enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")

	step, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, step)
	require.Equal(t, "docker.io", step.Target)
	require.Equal(t, PublishStepStateClaimed, step.State)
	require.Equal(t, 1, step.Attempts)

	// The gcr.io step belongs to another worker.
	next, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.Nil(t, next)

	// A worker that declares every target picks it up.
	svc.workerCfg.Targets = []string{"*"}
	step, err = svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, step)
	require.Equal(t, "gcr.io", step.Target)
}

func TestClaimStepIsExclusive(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0-enterprise": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	// The enterprise rule has a single destination, so exactly one step is
	// claimable.
	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0-enterprise")
	require.Len(t, unit.Steps, 1)

	// The first claim wins; every later claim of the same work (from this or
	// another worker) finds nothing. The guard is the state predicate of the
	// UPDATE, which is what makes it safe across processes; sqlite cannot
	// exercise concurrent writers here, so the check is sequential on purpose.
	first, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, first)

	workerID := svc.workerCfg.ID
	svc.workerCfg.ID = "other-worker"
	second, err := svc.claimStep(context.Background())
	svc.workerCfg.ID = workerID
	require.NoError(t, err)
	require.Nil(t, second, "a claimed step is invisible to other workers")
}

func TestClaimStepPrefersHigherPriority(t *testing.T) {
	svc := newPublishTestService(t, publishTestRules, nil)
	ctx := context.Background()
	now := svc.now()

	create := func(source string, priority int, createdAt time.Time) *ent.PublishUnit {
		unit, err := svc.db.PublishUnit.Create().
			SetID(svc.id()).
			SetKind(PublishKindImageCopy).
			SetArtifactURL("example.com/hub/pingcap/widget/image:v8.5.0").
			SetState(PublishStateQueued).
			SetSource(source).
			SetPriority(priority).
			SetCreatedAt(createdAt).
			Save(ctx)
		require.NoError(t, err)
		_, err = svc.db.PublishStep.Create().
			SetID(svc.id()).
			SetUnitID(unit.ID).
			SetTarget("docker.io").
			SetState(PublishStepStatePending).
			SetPriority(priority).
			SetPayload(map[string]any{}).
			SetCreatedAt(createdAt).
			Save(ctx)
		require.NoError(t, err)
		return unit
	}

	nightly := create("nightly", 10, now)
	release := create("api", 30, now.Add(time.Second))

	first, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.Equal(t, release.ID, first.UnitID, "the release step is claimed before the older nightly step")

	second, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.Equal(t, nightly.ID, second.UnitID)
}

func TestPublishSourcesAndPriorities(t *testing.T) {
	cases := []struct {
		requestedBy string
		source      string
		priority    int
	}{
		{"nightly", "nightly", 10},
		{"release", "api", 30},
		{"resume", "resume", 20},
		{"delivery-by-rules", "api", 20},
	}
	for _, tc := range cases {
		source, priority := publishSourceAndPriority(tc.requestedBy)
		require.Equal(t, tc.source, source, tc.requestedBy)
		require.Equal(t, tc.priority, priority, tc.requestedBy)
	}
}

func TestExecuteStepCopiesThenSkipsOnSecondRun(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	ctx := context.Background()

	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	svc.executeStep(ctx, claimOrFail(t, svc))
	svc.executeStep(ctx, claimOrFail(t, svc))

	stored, _, err := svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStateSucceeded, stored.State)
	require.NotNil(t, stored.FinishedAt)

	steps, err := svc.loadSteps(ctx, unit.ID)
	require.NoError(t, err)
	for _, step := range steps {
		require.Equal(t, PublishStepStateSucceeded, step.State)
		require.Equal(t, publishTestDigest, step.Result["digest"])
		require.Len(t, step.Result["copied"], 1)
	}
	require.Len(t, ops.copyList(), 2, "both destinations are copied once")

	// Re-running the same work is a no-op: the destinations already serve the
	// planned digest.
	second := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	svc.executeStep(ctx, claimOrFail(t, svc))
	svc.executeStep(ctx, claimOrFail(t, svc))

	steps, err = svc.loadSteps(ctx, second.ID)
	require.NoError(t, err)
	for _, step := range steps {
		require.Equal(t, PublishStepStateSucceeded, step.State)
		require.Empty(t, step.Result["copied"])
		require.Len(t, step.Result["skipped"], 1)
	}
	require.Len(t, ops.copyList(), 2, "nothing is copied twice")
}

func TestExecuteStepGivesUpAfterMaxAttempts(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	ops.copyErr = fmt.Errorf("registry rejected the push: 403")
	svc := newPublishWorkerTestService(t, ops, map[string]string{"max_attempts": "1"})
	ctx := context.Background()

	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	step := claimOrFail(t, svc)
	svc.executeStep(ctx, step)

	stored, err := svc.db.PublishStep.Get(ctx, step.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStepStateFailed, stored.State)
	require.Contains(t, stored.LastError, "403")
	require.NotNil(t, stored.FinishedAt)

	// One destination failed, one is still pending: the unit is not finished.
	storedUnit, _, err := svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStateQueued, storedUnit.State)
}

func TestExecuteStepRequeuesWithBackoff(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	ops.copyErr = fmt.Errorf("dial tcp: i/o timeout")
	svc := newPublishWorkerTestService(t, ops, map[string]string{"max_attempts": "3"})
	ctx := context.Background()

	enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	step := claimOrFail(t, svc)
	svc.executeStep(ctx, step)

	stored, err := svc.db.PublishStep.Get(ctx, step.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStepStatePending, stored.State)
	require.NotNil(t, stored.AvailableAt)
	require.True(t, stored.AvailableAt.After(svc.now()), "the retry is delayed")
	require.Empty(t, stored.ClaimedBy)
}

func TestPartialUnitAndRetryOfTheFailedStep(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, map[string]string{"max_attempts": "1"})
	ctx := context.Background()

	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")

	// First destination succeeds.
	first := claimOrFail(t, svc)
	svc.executeStep(ctx, first)

	// Second destination fails for good.
	ops.copyErr = fmt.Errorf("gcr.io is unreachable")
	second := claimOrFail(t, svc)
	svc.executeStep(ctx, second)

	storedUnit, _, err := svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStatePartial, storedUnit.State)
	require.NotNil(t, storedUnit.FinishedAt)

	// Retry re-queues only the failed destination.
	ops.copyErr = nil
	retried, err := svc.RetryUnit(ctx, &publish.RetryUnitPayload{ID: unit.ID})
	require.NoError(t, err)
	require.Equal(t, publish.PublishState(PublishStateQueued), retried.State)

	next := claimOrFail(t, svc)
	require.Equal(t, second.ID, next.ID)
	svc.executeStep(ctx, next)

	storedUnit, _, err = svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStateSucceeded, storedUnit.State)

	// The destination that already succeeded was not copied again; the failed
	// one was tried once during the first pass and once after the retry.
	copies := ops.copyList()
	require.Len(t, copies, 3)
	require.Equal(t, 1, countPrefix(copies, "-> "+first.Dest))
	require.Equal(t, 2, countPrefix(copies, "-> "+second.Dest))
}

func TestReaperReturnsExpiredClaimsToPending(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	ctx := context.Background()

	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	step := claimOrFail(t, svc)

	// Simulate a worker that died mid-copy.
	past := time.Now().Add(-time.Minute)
	_, err := svc.db.PublishStep.UpdateOneID(step.ID).SetLeaseUntil(past).Save(ctx)
	require.NoError(t, err)

	reclaimed, err := svc.reapExpiredSteps(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, reclaimed)

	stored, err := svc.db.PublishStep.Get(ctx, step.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStepStatePending, stored.State)
	require.Empty(t, stored.ClaimedBy)
	require.Nil(t, stored.LeaseUntil)

	storedUnit, _, err := svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStateQueued, storedUnit.State)
}

func TestAggregateStepStates(t *testing.T) {
	steps := func(states ...string) []*ent.PublishStep {
		out := make([]*ent.PublishStep, 0, len(states))
		for _, state := range states {
			out = append(out, &ent.PublishStep{State: state})
		}
		return out
	}
	tests := map[string]struct {
		steps []*ent.PublishStep
		want  string
	}{
		"all pending":       {steps(PublishStepStatePending, PublishStepStatePending), PublishStateQueued},
		"one claimed":       {steps(PublishStepStateClaimed, PublishStepStatePending), PublishStateRunning},
		"all succeeded":     {steps(PublishStepStateSucceeded, PublishStepStateSucceeded), PublishStateSucceeded},
		"mixed":             {steps(PublishStepStateSucceeded, PublishStepStateFailed), PublishStatePartial},
		"all failed":        {steps(PublishStepStateFailed, PublishStepStateSkipped), PublishStateFailed},
		"failed then retry": {steps(PublishStepStateSucceeded, PublishStepStatePending), PublishStateQueued},
	}
	for name, tt := range tests {
		require.Equal(t, tt.want, aggregateStepStates(tt.steps), name)
	}
}

func claimOrFail(t *testing.T, svc *publishsrvc) *ent.PublishStep {
	t.Helper()
	step, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, step, "a step should be claimable")
	return step
}

func countPrefix(values []string, substr string) int {
	count := 0
	for _, value := range values {
		if strings.Contains(value, substr) {
			count++
		}
	}
	return count
}

// TestStepStateIsPersistedOnClaim guards the only column the reaper relies on.
func TestStepStateIsPersistedOnClaim(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	ctx := context.Background()
	enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")

	step := claimOrFail(t, svc)
	require.NotNil(t, step.LeaseUntil)
	require.True(t, step.LeaseUntil.After(time.Now()))

	count, err := svc.db.PublishStep.Query().
		Where(entpublishstep.State(PublishStepStateClaimed)).
		Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)
}

func TestConsumePublishStepsDrainsTheUnit(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	ctx := context.Background()

	unit := enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")
	svc.consumePublishSteps(ctx)

	stored, _, err := svc.loadUnit(ctx, unit.ID)
	require.NoError(t, err)
	require.Equal(t, PublishStateSucceeded, stored.State)

	// Nothing left: a second drain is a no-op.
	svc.consumePublishSteps(ctx)
	require.Len(t, ops.copyList(), 2)
}

func TestStartHonoursTheConfiguredKinds(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})

	t.Run("unknown kind returns immediately", func(t *testing.T) {
		svc := newPublishWorkerTestService(t, ops, nil)
		svc.workerCfg.Kinds = []string{"unknown"}

		done := make(chan struct{})
		go func() {
			svc.Start(context.Background())
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("Start did not return for a disabled kind")
		}
		require.Empty(t, ops.copyList())
	})

	t.Run("tiup kind keeps the claim loop running", func(t *testing.T) {
		svc := newPublishWorkerTestService(t, ops, nil)
		svc.workerCfg.Kinds = []string{PublishKindTiUP}
		svc.workerCfg.PollInterval = "10ms"

		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() {
			svc.Start(ctx)
			close(done)
		}()

		// The loop is alive: it does not return while the context is open.
		select {
		case <-done:
			t.Fatal("Start returned for the tiup kind")
		case <-time.After(100 * time.Millisecond):
		}
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("Start did not stop after the context was cancelled")
		}
	})
}

func TestRenewStepLeaseIsFencedByTheToken(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	ctx := context.Background()
	enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")

	step := claimOrFail(t, svc)
	before := *step.LeaseUntil

	ok, err := svc.renewStepLease(ctx, step.ID, step.ClaimedBy)
	require.NoError(t, err)
	require.True(t, ok)

	renewed, err := svc.db.PublishStep.Get(ctx, step.ID)
	require.NoError(t, err)
	require.False(t, renewed.LeaseUntil.Before(before), "the lease moved forward")

	ok, err = svc.renewStepLease(ctx, step.ID, "someone-else")
	require.NoError(t, err)
	require.False(t, ok, "a foreign token cannot renew the lease")

	// A worker that lost the lease sees renew=false, which cancels its work.
	ok, err = svc.renewStepLease(ctx, step.ID, step.ClaimedBy)
	require.NoError(t, err)
	require.True(t, ok)
	require.NoError(t, svc.db.PublishStep.UpdateOneID(step.ID).SetState(PublishStepStatePending).Exec(ctx))
	ok, err = svc.renewStepLease(ctx, step.ID, step.ClaimedBy)
	require.NoError(t, err)
	require.False(t, ok)
}

func TestPublishBackoffGrowsAndCaps(t *testing.T) {
	svc := &publishsrvc{}

	// +/-20% jitter around an exponential base, capped at the maximum.
	tests := map[int][2]time.Duration{
		1:  {8 * time.Second, 12 * time.Second},
		2:  {16 * time.Second, 24 * time.Second},
		3:  {32 * time.Second, 48 * time.Second},
		10: {publishBackoffMax * 4 / 5, publishBackoffMax * 6 / 5},
	}
	for attempts, bounds := range tests {
		for i := 0; i < 20; i++ {
			d := svc.publishBackoff(attempts)
			require.GreaterOrEqual(t, d, bounds[0], "attempt %d", attempts)
			require.LessOrEqual(t, d, bounds[1], "attempt %d", attempts)
		}
	}
}

func TestNewPublishWiresTheService(t *testing.T) {
	logger := zerolog.Nop()
	cfg := &config.Service{
		Store:   config.Store{Driver: "sqlite3", DSN: "file:new_publish?mode=memory&cache=shared&_fk=1"},
		Publish: config.Publish{DeliveryConfigPath: "/tmp/does-not-exist.yaml"},
	}
	svc := NewPublish(&logger, cfg)
	require.NotNil(t, svc)

	// Stop returns immediately when nothing is in flight.
	svc.(*publishsrvc).Stop()

	// A bad store configuration yields nil instead of panicking.
	require.Nil(t, NewPublish(&logger, &config.Service{Store: config.Store{Driver: "mysql", DSN: "invalid"}}))
}

func TestRunPublishReaperStopsWithTheContext(t *testing.T) {
	svc := newPublishWorkerTestService(t, newRegistryStub(nil), nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan struct{})
	go func() {
		svc.runPublishReaper(ctx)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the reaper did not stop with the context")
	}
}

func TestHeartbeatStepRenewsAndThenAborts(t *testing.T) {
	ops := newRegistryStub(map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	})
	svc := newPublishWorkerTestService(t, ops, nil)
	svc.workerCfg.Heartbeat = "10ms"
	svc.workerCfg.Lease = "1s"
	ctx := context.Background()
	enqueue(t, svc, "example.com/hub/pingcap/widget/image:v8.5.0")

	step := claimOrFail(t, svc)
	before := *step.LeaseUntil

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go svc.heartbeatStep(runCtx, cancel, step.ID, step.ClaimedBy)

	require.Eventually(t, func() bool {
		stored, err := svc.db.PublishStep.Get(ctx, step.ID)
		if err != nil {
			return false
		}
		return stored.LeaseUntil != nil && stored.LeaseUntil.After(before)
	}, 5*time.Second, 10*time.Millisecond, "the lease is renewed")

	// Losing the claim (another worker took over, or the reaper reset the row)
	// cancels the running copy.
	require.NoError(t, svc.db.PublishStep.UpdateOneID(step.ID).
		SetState(PublishStepStatePending).
		ClearClaimedBy().
		Exec(ctx))

	require.Eventually(t, func() bool {
		return runCtx.Err() != nil
	}, 5*time.Second, 10*time.Millisecond, "a lost lease cancels the execution")
}

func TestValidatePublishTargets(t *testing.T) {
	rulesPath := t.TempDir() + "/delivery.yaml"
	require.NoError(t, os.WriteFile(rulesPath, []byte(publishTestRules), 0o644))

	base := func() *config.Service {
		return &config.Service{Publish: config.Publish{DeliveryConfigPath: rulesPath}}
	}

	// Wildcard and concrete-but-known targets are accepted.
	require.NoError(t, ValidatePublishTargets(base()))
	cfg := base()
	cfg.Publish.Targets = []string{"docker.io", "gcr.io"}
	require.NoError(t, ValidatePublishTargets(cfg))

	// A typo is rejected with the known catalogue.
	cfg = base()
	cfg.Publish.Targets = []string{"grc.io"}
	err := ValidatePublishTargets(cfg)
	require.Error(t, err)
	require.Contains(t, err.Error(), "grc.io")

	// Publish targets win over worker targets, so the image sync queue is not
	// re-sharded by accident.
	cfg = base()
	cfg.Worker.Targets = []string{"*"}
	cfg.Publish.Targets = []string{"docker.io"}
	require.NoError(t, ValidatePublishTargets(cfg))

	// Missing rules file is reported separately so the caller can warn instead
	// of crashing before Flux syncs the ConfigMap.
	cfg = &config.Service{Publish: config.Publish{
		DeliveryConfigPath: rulesPath + ".missing",
		Targets:            []string{"docker.io"},
	}}
	require.ErrorIs(t, ValidatePublishTargets(cfg), ErrPublishRulesUnavailable)
}

func TestPublishTargetsOverrideWorkerTargets(t *testing.T) {
	logger := zerolog.Nop()
	cfg := &config.Service{Worker: config.Worker{Targets: []string{"*"}}, Publish: config.Publish{Targets: []string{"docker.io"}}}
	svc := newPublishService(&logger, nil, cfg)
	require.Equal(t, []string{"docker.io"}, svc.workerCfg.Targets)

	cfg.Publish.Targets = nil
	svc = newPublishService(&logger, nil, cfg)
	require.Equal(t, []string{"*"}, svc.workerCfg.Targets)
}
