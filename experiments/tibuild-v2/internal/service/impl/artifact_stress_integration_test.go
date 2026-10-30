//go:build integration

package impl

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// startMySQL starts a throwaway MySQL instance for multi-worker concurrency
// tests. SQLite cannot model MySQL row-lock semantics, so this test is tagged
// "integration" and is not part of the default unit-test run.
func startMySQL(t *testing.T) *ent.Client {
	t.Helper()
	ctx := context.Background()

	request := testcontainers.ContainerRequest{
		Image:        "mysql:8.0",
		ExposedPorts: []string{"3306/tcp"},
		Env: map[string]string{
			"MYSQL_ROOT_PASSWORD": "root",
			"MYSQL_DATABASE":      "tibuild",
		},
		WaitingFor: wait.ForLog("ready for connections").
			WithOccurrence(2).
			WithStartupTimeout(3 * time.Minute),
	}
	container, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
		ContainerRequest: request,
		Started:          true,
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = container.Terminate(context.Background()) })

	host, err := container.Host(ctx)
	require.NoError(t, err)
	port, err := container.MappedPort(ctx, "3306/tcp")
	require.NoError(t, err)

	dsn := fmt.Sprintf("root:root@tcp(%s:%s)/tibuild?parseTime=true&loc=UTC", host, port.Port())

	deadline := time.Now().Add(2 * time.Minute)
	var db *ent.Client
	for {
		db, err = ent.Open("mysql", dsn)
		if err == nil {
			err = db.Schema.Create(ctx)
			if err == nil {
				break
			}
			_ = db.Close()
		}
		if time.Now().After(deadline) {
			t.Fatalf("mysql did not become ready: %v", err)
		}
		time.Sleep(2 * time.Second)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestWorkerMultiReplicaAndLeaseRecoveryMySQL(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping MySQL integration test in short mode")
	}
	ctx := context.Background()
	db := startMySQL(t)

	const taskCount = 1000
	builders := make([]*ent.ImageSyncTaskCreate, taskCount)
	for i := range builders {
		builders[i] = db.ImageSyncTask.Create().
			SetSource(fmt.Sprintf("example.invalid/src:%d", i)).
			SetTarget(fmt.Sprintf("example.invalid/dst:%d", i)).
			SetTargetKey("docker.io").
			SetStatus(TaskStatusPending)
	}
	require.NoError(t, db.ImageSyncTask.CreateBulk(builders...).Exec(ctx))

	var (
		mu       sync.Mutex
		claims   = make(map[int]int)
		executed = make(map[int]int)
	)
	errCh := make(chan error, 16)

	var wg sync.WaitGroup
	for workerIndex := 0; workerIndex < 3; workerIndex++ {
		worker := newLeaseTestService(t, db, config.Worker{
			ID:          fmt.Sprintf("worker-%d", workerIndex),
			Targets:     []string{"*"},
			Concurrency: 4,
			Lease:       "90s",
			Heartbeat:   "30s",
		})
		for slot := 0; slot < worker.workerConfig().Concurrency; slot++ {
			wg.Add(1)
			go func(worker *artifactsrvc) {
				defer wg.Done()
				for {
					task, err := worker.claimOne(ctx)
					if err != nil {
						errCh <- err
						return
					}
					if task == nil {
						return
					}
					mu.Lock()
					claims[task.ID]++
					mu.Unlock()

					ok, err := worker.markSucceeded(ctx, task)
					if err != nil {
						errCh <- err
						return
					}
					if !ok {
						errCh <- fmt.Errorf("task %d lost its lease before the result was written", task.ID)
						return
					}
					mu.Lock()
					executed[task.ID]++
					mu.Unlock()
				}
			}(worker)
		}
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		require.NoError(t, err)
	}

	require.Len(t, claims, taskCount)
	for id, count := range claims {
		require.Equalf(t, 1, count, "task %d was claimed %d times", id, count)
	}
	require.Len(t, executed, taskCount)
	for id, count := range executed {
		require.Equalf(t, 1, count, "task %d was executed %d times", id, count)
	}

	residual, err := db.ImageSyncTask.Query().
		Where(entimagesynctask.Status(TaskStatusProcessing)).
		Count(ctx)
	require.NoError(t, err)
	require.Zero(t, residual, "no PROCESSING row may remain after the stress run")

	// Pod-kill simulation: claim with a short lease, never heartbeat, let the
	// reaper reclaim it, then let another worker finish it.
	victim := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	dyingWorker := newLeaseTestService(t, db, config.Worker{
		ID: "worker-doomed", Targets: []string{"*"}, Lease: "2s", Heartbeat: "1s",
	})
	claimed, err := dyingWorker.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, victim.ID, claimed.ID)

	time.Sleep(2500 * time.Millisecond)

	recoveryWorker := newLeaseTestService(t, db, config.Worker{
		ID: "worker-recovery", Targets: []string{"*"}, Lease: "90s", Heartbeat: "30s",
	})
	recoveryWorker.ensureObservability()
	n, err := recoveryWorker.reapAndRecord(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, n)

	reclaimed, err := recoveryWorker.claimOne(ctx)
	require.NoError(t, err)
	require.NotNil(t, reclaimed)
	require.Equal(t, victim.ID, reclaimed.ID)
	ok, err := recoveryWorker.markSucceeded(ctx, reclaimed)
	require.NoError(t, err)
	require.True(t, ok)

	finalTask, err := db.ImageSyncTask.Get(ctx, victim.ID)
	require.NoError(t, err)
	require.Equal(t, TaskStatusSucceeded, finalTask.Status)

	body := scrapeMetrics(t, recoveryWorker)
	require.Contains(t, body, "publish_task_lease_expired_total 1")
}

// Five workers across replicas share one queue, so the alert election must pick
// exactly one evaluator even when they all tick at the same moment.
func TestAlertLeaseSingleLeaderMySQL(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping MySQL integration test in short mode")
	}
	ctx := context.Background()
	db := startMySQL(t)
	now := time.Now()

	type leaseResult struct {
		leader bool
		err    error
	}
	services := make([]*artifactsrvc, 0, 5)
	for i := 0; i < 5; i++ {
		s := newLeaseTestService(t, db, config.Worker{ID: fmt.Sprintf("worker-%d", i)})
		s.nowFn = func() time.Time { return now }
		services = append(services, s)
	}

	results := make(chan leaseResult, len(services))
	var wg sync.WaitGroup
	for _, svc := range services {
		wg.Add(1)
		go func(s *artifactsrvc) {
			defer wg.Done()
			leader, err := s.acquireAlertLease(ctx, now)
			results <- leaseResult{leader: leader, err: err}
		}(svc)
	}
	wg.Wait()
	close(results)

	leaders := 0
	for res := range results {
		require.NoError(t, res.err)
		if res.leader {
			leaders++
		}
	}
	require.Equal(t, 1, leaders, "exactly one of the five workers may win the election")

	// The winner keeps the lease on the next tick; the losers stay followers.
	second := make(map[string]int)
	for _, svc := range services {
		leader, err := svc.acquireAlertLease(ctx, now.Add(5*time.Second))
		require.NoError(t, err)
		if leader {
			second[svc.workerConfig().ID] = 1
		}
	}
	require.Len(t, second, 1, "the leader must stay stable while its lease is live")
}
