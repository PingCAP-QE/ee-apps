package impl

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entalertlease "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/alertlease"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// newAlertLeaseTestService builds a worker with an explicit identity and a
// frozen clock so lease expiry is deterministic.
func newAlertLeaseTestService(t *testing.T, db *ent.Client, id string, now time.Time) *artifactsrvc {
	t.Helper()
	s := newLeaseTestService(t, db, config.Worker{ID: id})
	s.nowFn = func() time.Time { return now }
	s.ensureObservability()
	return s
}

func TestAlertLeaseElectsSingleLeader(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()
	a := newAlertLeaseTestService(t, db, "worker-a", now)
	b := newAlertLeaseTestService(t, db, "worker-b", now)

	leader, err := a.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.True(t, leader, "the first worker acquires the lease")

	leader, err = b.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.False(t, leader, "a live lease must not be taken over")

	leader, err = a.acquireAlertLease(ctx, now.Add(5*time.Second))
	require.NoError(t, err)
	require.True(t, leader, "the holder renews its own lease")
}

// The lease TTL is 2 x worker.metrics_interval, so an evicted holder must lose
// the election to the next worker instead of keeping the alert duties.
func TestAlertLeaseTakeoverAfterExpiry(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()
	a := newAlertLeaseTestService(t, db, "worker-a", now)
	b := newAlertLeaseTestService(t, db, "worker-b", now)

	leader, err := a.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.True(t, leader)

	leader, err = b.acquireAlertLease(ctx, now.Add(31*time.Second))
	require.NoError(t, err)
	require.True(t, leader, "an expired lease is taken over")

	leader, err = a.acquireAlertLease(ctx, now.Add(32*time.Second))
	require.NoError(t, err)
	require.False(t, leader, "the evicted holder cannot renew someone else's lease")
}

// A short metrics_interval must shorten the election TTL accordingly, so an e2e
// run does not have to wait for the production 30s window.
func TestAlertLeaseTTLFollowsMetricsInterval(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()
	cfg := config.Worker{ID: "worker-a", MetricsInterval: "5s"}
	s := newLeaseTestService(t, db, cfg)
	s.nowFn = func() time.Time { return now }
	s.ensureObservability()

	leader, err := s.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.True(t, leader)

	peer := newLeaseTestService(t, db, config.Worker{ID: "worker-b", MetricsInterval: "5s"})
	leader, err = peer.acquireAlertLease(ctx, now.Add(11*time.Second))
	require.NoError(t, err)
	require.True(t, leader, "a 5s interval expires the lease after 10s")
}

func TestOnlyElectedLeaderSendsAlerts(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()

	task := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	_, err := task.Update().SetCreatedAt(now.Add(-31 * time.Minute)).Save(ctx)
	require.NoError(t, err)

	notifier := &fakeAlertNotifier{}
	services := make([]*artifactsrvc, 0, 5)
	for _, id := range []string{"worker-1", "worker-2", "worker-3", "worker-4", "worker-5"} {
		s := newAlertLeaseTestService(t, db, id, now)
		s.alerter = notifier
		services = append(services, s)
	}

	for _, s := range services {
		s.refreshMetricsAndAlerts(ctx)
	}

	require.Equal(t, 1, notifier.count(),
		"five workers sharing the queue must send the same alert only once")
}

// The schema is created on every startup, so the new alert_leases table must be
// additive and keep its row.
func TestSchemaCreateIsIdempotentForAlertLeases(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()

	_, err := db.AlertLease.Create().
		SetName(alertLeaseName).
		SetHolder("worker-a").
		SetLeaseUntil(now.Add(time.Minute)).
		Save(ctx)
	require.NoError(t, err)

	require.NoError(t, db.Schema.Create(ctx))
	require.NoError(t, db.Schema.Create(ctx))

	lease, err := db.AlertLease.Query().Where(entalertlease.Name(alertLeaseName)).Only(ctx)
	require.NoError(t, err)
	require.Equal(t, "worker-a", lease.Holder)
	require.False(t, lease.LeaseUntil.IsZero())
}

// A database failure must not be mistaken for "another worker is the leader".
func TestAlertLeaseReturnsDatabaseError(t *testing.T) {
	db := serverlessTestDB(t)
	now := time.Now()
	s := newAlertLeaseTestService(t, db, "worker-a", now)
	require.NoError(t, db.Close())

	leader, err := s.acquireAlertLease(context.Background(), now)
	require.Error(t, err)
	require.False(t, leader)
}

// Two workers can pass the existence check at the same time; the loser sees a
// unique-constraint error and must simply wait for the next tick.
func TestAlertLeaseTreatsCreateConflictAsLostElection(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()
	s := newAlertLeaseTestService(t, db, "worker-a", now)

	db.AlertLease.Use(func(next ent.Mutator) ent.Mutator {
		return ent.MutateFunc(func(ctx context.Context, m ent.Mutation) (ent.Value, error) {
			if m.Op() == ent.OpCreate {
				return nil, &ent.ConstraintError{}
			}
			return next.Mutate(ctx, m)
		})
	})

	leader, err := s.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.False(t, leader, "losing the insert race must not make this worker the leader")
}

// fakeRecipientNotifier models a Lark notifier that may or may not have rows in
// lark.channels.
type fakeRecipientNotifier struct {
	fakeAlertNotifier
	recipients bool
}

func (f *fakeRecipientNotifier) HasRecipients() bool { return f.recipients }

// The reaper runs on every worker, but only the elected leader sends alerts.
// A reclaim performed by a follower must therefore still reach the leader,
// otherwise the lease alert is lost whenever the follower wins the reaper race.
func TestLeaseReclaimSeenByLeaderWhenAnotherWorkerReaps(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()

	leader := newAlertLeaseTestService(t, db, "worker-leader", now)
	notifier := &fakeRecipientNotifier{recipients: true}
	leader.alerter = notifier
	isLeader, err := leader.acquireAlertLease(ctx, now)
	require.NoError(t, err)
	require.True(t, isLeader)

	// A different worker reclaims an expired lease. It does not hold the alert
	// lease, so it must not notify anyone itself.
	follower := newAlertLeaseTestService(t, db, "worker-follower", now)
	follower.alerter = &fakeRecipientNotifier{recipients: true}
	task := createLeaseTestTask(t, db, "docker.io", TaskStatusProcessing)
	_, err = task.Update().
		SetClaimedBy("gone").
		SetLeaseUntil(now.Add(-time.Minute)).
		SetAvailableAt(now.Add(time.Hour)).
		Save(ctx)
	require.NoError(t, err)
	n, err := follower.reapAndRecord(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, n)

	leader.refreshMetricsAndAlerts(ctx)
	require.Equal(t, 1, notifier.count(),
		"the leader must report a lease reclaim performed by another worker")
}

func TestWorkerWithoutRecipientsSkipsTheElection(t *testing.T) {
	ctx := context.Background()
	db := serverlessTestDB(t)
	now := time.Now()

	task := createLeaseTestTask(t, db, "docker.io", TaskStatusPending)
	_, err := task.Update().SetCreatedAt(now.Add(-31 * time.Minute)).Save(ctx)
	require.NoError(t, err)

	silent := newAlertLeaseTestService(t, db, "worker-gcp", now)
	silent.alerter = &fakeRecipientNotifier{recipients: false}
	silent.refreshMetricsAndAlerts(ctx)

	require.False(t, silent.canEvaluateAlerts())
	count, err := db.AlertLease.Query().Count(ctx)
	require.NoError(t, err)
	require.Zero(t, count, "a worker without recipients must not hold the alert lease")

	talker := newAlertLeaseTestService(t, db, "worker-prod2", now)
	notifier := &fakeRecipientNotifier{recipients: true}
	talker.alerter = notifier
	talker.refreshMetricsAndAlerts(ctx)
	require.Equal(t, 1, notifier.count(), "the worker with recipients delivers the alert")
}
