package impl

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func newLeaseTestService(t *testing.T, db *ent.Client, workerCfg config.Worker) *artifactsrvc {
	t.Helper()
	workerCfg.ApplyDefaults()
	logger := zerolog.Nop()
	return &artifactsrvc{
		logger:      &logger,
		db:          db,
		workerCfg:   workerCfg,
		pollingRate: time.Hour,
	}
}

func createLeaseTestTask(t *testing.T, db *ent.Client, targetKey, status string) *ent.ImageSyncTask {
	t.Helper()
	create := db.ImageSyncTask.Create().
		SetSource("hub.pingcap.net/pingcap/tidb:v1.0.0-20260101").
		SetTarget("docker.io/pingcap/tidb:v1.0.0-20260101").
		SetStatus(status)
	if targetKey != "" {
		create.SetTargetKey(targetKey)
	}
	task, err := create.Save(context.Background())
	require.NoError(t, err)
	return task
}

func TestSchemaCreateIsIdempotentWithExistingRows(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	task := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)

	require.NoError(t, db.Schema.Create(ctx))
	require.NoError(t, db.Schema.Create(ctx))

	stored, err := db.ImageSyncTask.Get(ctx, task.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusPending, stored.Status)
	require.Equal(t, "docker.io", stored.TargetKey)
}

func TestClaimOneSetsLease(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{
		ID: "worker-a", Targets: []string{"*"}, Lease: "90s", Heartbeat: "30s", MaxAttempts: 3,
	})
	created := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)

	before := time.Now()
	claimed, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, created.ID, claimed.ID)
	require.Equal(t, TaskStatusProcessing, claimed.Status)
	require.Contains(t, claimed.ClaimedBy, "worker-a")
	require.Equal(t, 1, claimed.Attempts)
	require.True(t, claimed.LeaseUntil.After(before))
	require.True(t, claimed.AvailableAt.IsZero())

	next, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.Nil(t, next)
}

func TestClaimOneRespectsTargetsAndAvailability(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{
		ID: "worker-a", Targets: []string{"gcr.io"}, Lease: "90s", Heartbeat: "30s",
	})
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)

	claimed, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.Nil(t, claimed, "worker must not claim another target's task")

	createLeaseTestTask(t, db, "gcr.io", TaskStatusPending)
	claimed, err = s.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, "gcr.io", claimed.TargetKey)

	// A future available_at delays the claim; a past one does not.
	future := createLeaseTestTask(t, db, "gcr.io", TaskStatusPending)
	_, err = future.Update().SetAvailableAt(time.Now().Add(time.Hour)).Save(ctx)
	require.NoError(t, err)
	claimed, err = s.claimOne(ctx)
	require.NoError(t, err)
	require.Nil(t, claimed)

	_, err = future.Update().SetAvailableAt(time.Now().Add(-time.Minute)).Save(ctx)
	require.NoError(t, err)
	claimed, err = s.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, future.ID, claimed.ID)
}

func TestClaimOneConcurrentOnlyOneWinner(t *testing.T) {
	ctx := context.Background()
	dsn := filepath.Join(t.TempDir(), "claim.db") + "?_busy_timeout=5000&_journal_mode=WAL&_fk=1"
	db, err := ent.Open("sqlite3", dsn)
	require.NoError(t, err)
	require.NoError(t, db.Schema.Create(ctx))
	t.Cleanup(func() { _ = db.Close() })
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)

	workers := []*artifactsrvc{
		newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "30s"}),
		newLeaseTestService(t, db, config.Worker{ID: "worker-b", Lease: "90s", Heartbeat: "30s"}),
	}

	var wg sync.WaitGroup
	results := make([]*ent.ImageSyncTask, len(workers))
	errs := make([]error, len(workers))
	for i, worker := range workers {
		wg.Add(1)
		go func(i int, worker *artifactsrvc) {
			defer wg.Done()
			results[i], errs[i] = worker.claimOne(ctx)
		}(i, worker)
	}
	wg.Wait()

	winners := 0
	for i := range results {
		require.NoError(t, errs[i])
		if results[i] != nil {
			winners++
		}
	}
	require.Equal(t, 1, winners, "exactly one worker may claim the task")
}

func TestRenewLeaseAndFencing(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "30s"})
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	task, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, task)

	ok, err := s.renewLease(ctx, task.ID, "forged-token", 90*time.Second)
	require.NoError(t, err)
	require.False(t, ok)

	ok, err = s.renewLease(ctx, task.ID, task.ClaimedBy, 120*time.Second)
	require.NoError(t, err)
	require.True(t, ok)

	forged := *task
	forged.ClaimedBy = "forged-token"
	ok, err = s.markSucceeded(ctx, &forged)
	require.NoError(t, err)
	require.False(t, ok, "forged task token must not write results")
	stored, err := db.ImageSyncTask.Get(ctx, task.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusProcessing, stored.Status)

	task.ClaimedBy = stored.ClaimedBy
	ok, err = s.markSucceeded(ctx, task)
	require.NoError(t, err)
	require.True(t, ok)
	stored, err = db.ImageSyncTask.Get(ctx, task.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusSucceeded, stored.Status)
	require.Empty(t, stored.ClaimedBy)
	require.True(t, stored.LeaseUntil.IsZero())
}

func TestReapOnceReclaimsOnlyExpiredLeases(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "30s"})

	expired := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err := expired.Update().
		SetClaimedBy("worker-old").
		SetLeaseUntil(time.Now().Add(-time.Minute)).
		Save(ctx)
	require.NoError(t, err)

	live := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err = live.Update().
		SetClaimedBy("worker-live").
		SetLeaseUntil(time.Now().Add(time.Minute)).
		Save(ctx)
	require.NoError(t, err)

	n, err := s.reapOnce(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, n)

	reclaimed, err := db.ImageSyncTask.Get(ctx, expired.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusPending, reclaimed.Status)
	require.Empty(t, reclaimed.ClaimedBy)
	require.True(t, reclaimed.LeaseUntil.IsZero())

	stillLive, err := db.ImageSyncTask.Get(ctx, live.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusProcessing, stillLive.Status)
	require.Equal(t, "worker-live", stillLive.ClaimedBy)
}

func TestHandleFailureBackoffAndMaxAttempts(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "30s", MaxAttempts: 3})

	retryTask := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	claimed, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.Equal(t, retryTask.ID, claimed.ID)

	ok, err := s.handleFailure(ctx, claimed, errors.New("copy failed"))
	require.NoError(t, err)
	require.True(t, ok)
	stored, err := db.ImageSyncTask.Get(ctx, claimed.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusPending, stored.Status)
	require.Equal(t, "copy failed", stored.ErrMsg)
	require.Equal(t, 1, stored.RetryCount)
	require.Empty(t, stored.ClaimedBy)
	require.True(t, stored.AvailableAt.After(time.Now()))

	finalTask := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err = finalTask.Update().
		SetClaimedBy("worker-a").
		SetAttempts(3).
		SetLeaseUntil(time.Now().Add(time.Minute)).
		Save(ctx)
	require.NoError(t, err)
	finalTask, err = db.ImageSyncTask.Get(ctx, finalTask.ID)
	require.NoError(t, err)

	ok, err = s.handleFailure(ctx, finalTask, errors.New("still failing"))
	require.NoError(t, err)
	require.True(t, ok)
	stored, err = db.ImageSyncTask.Get(ctx, finalTask.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusFailed, stored.Status)
	require.Empty(t, stored.ClaimedBy)
}

func TestHeartbeatCancelsWhenLeaseLost(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	s := newLeaseTestService(t, db, config.Worker{ID: "worker-a", Lease: "90s", Heartbeat: "10ms"})
	createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	task, err := s.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, task)

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.heartbeat(runCtx, cancel, task.ID, task.ClaimedBy)
	}()

	// Simulate the reaper reclaiming the task while the copy is still running.
	_, err = db.ImageSyncTask.UpdateOneID(task.ID).
		SetStatus(TaskStatusPending).
		ClearClaimedBy().
		ClearLeaseUntil().
		Save(ctx)
	require.NoError(t, err)

	select {
	case <-runCtx.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("heartbeat did not cancel the process after losing the lease")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("heartbeat goroutine did not stop")
	}
}

func TestBackoffIsBounded(t *testing.T) {
	s := &artifactsrvc{}
	first := s.backoff(1)
	require.GreaterOrEqual(t, first, 8*time.Second)
	require.LessOrEqual(t, first, 12*time.Second)
	for attempts := 1; attempts <= 20; attempts++ {
		require.LessOrEqual(t, s.backoff(attempts), 6*time.Minute)
	}
}

func TestImageTargetKey(t *testing.T) {
	tests := map[string]string{
		"docker.io/pingcap/tidb:v1.0.0":             "docker.io",
		"pingcap/tidb:v1.0.0":                       "docker.io",
		"gcr.io/pingcap-public/dbaas/foo:v1.0.0":    "gcr.io",
		"localhost:5000/test/image:tag":             "localhost:5000",
		"us-docker.pkg.dev/project/repo/img:latest": "us-docker.pkg.dev",
		"not a valid reference":                     "",
	}
	for image, want := range tests {
		require.Equal(t, want, imageTargetKey(image), image)
	}
}
