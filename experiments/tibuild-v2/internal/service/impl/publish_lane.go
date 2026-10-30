package impl

import (
	"context"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishlane "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishlane"
)

// Lane names. A lane serializes publication intents of one kind: two TiUP
// mirrors must never be published concurrently, and a unit's steps must not
// interleave with another unit's steps, so the TiUP kind owns exactly one lane.
const (
	PublishLaneTiUP = "tiup"
)

// publishLane is the single-row semaphore that serializes publish units of one
// kind.
//
// Ownership belongs to the unit, not to the process: the worker that finishes
// one step makes the next step claimable, and the worker that picks it up
// renews the same row. Every mutation is fenced on (unit, holder), so a worker
// that lost the lease cannot extend it or report success afterwards.
type publishLane struct {
	db    *ent.Client
	name  string
	nowFn func() time.Time
}

func newPublishLane(db *ent.Client, name string) *publishLane {
	return &publishLane{db: db, name: name, nowFn: time.Now}
}

func (l *publishLane) now() time.Time {
	if l.nowFn != nil {
		return l.nowFn()
	}
	return time.Now()
}

// ensure creates the lane row once. It is safe to call concurrently: the unique
// index on `lane` turns a lost race into a constraint error, which is ignored.
func (l *publishLane) ensure(ctx context.Context) error {
	_, err := l.db.PublishLane.Query().
		Where(entpublishlane.Lane(l.name)).
		Only(ctx)
	if err == nil {
		return nil
	}
	if !ent.IsNotFound(err) {
		return err
	}

	_, err = l.db.PublishLane.Create().
		SetID(1).
		SetLane(l.name).
		Save(ctx)
	if err != nil && !ent.IsConstraintError(err) {
		return err
	}
	return nil
}

// Acquire takes the lane for unitID, unless another unit still holds a live
// lease. It returns false when the lane is busy; the caller requeues the step.
func (l *publishLane) Acquire(ctx context.Context, unitID, holder string, lease time.Duration) (bool, error) {
	if err := l.ensure(ctx); err != nil {
		return false, err
	}
	now := l.now()

	updated, err := l.db.PublishLane.Update().
		Where(
			entpublishlane.Lane(l.name),
			entpublishlane.Or(
				entpublishlane.UnitID(""),
				entpublishlane.LeaseUntilIsNil(),
				entpublishlane.LeaseUntilLT(now),
			),
		).
		SetUnitID(unitID).
		SetHolder(holder).
		SetLeaseUntil(now.Add(lease)).
		SetUpdatedAt(now).
		Save(ctx)
	if err != nil {
		return false, err
	}
	return updated == 1, nil
}

// Renew extends the lease of the unit that still holds it. A false return means
// the lane was lost: the caller must stop and must not write any result.
func (l *publishLane) Renew(ctx context.Context, unitID, holder string, lease time.Duration) (bool, error) {
	now := l.now()

	updated, err := l.db.PublishLane.Update().
		Where(
			entpublishlane.Lane(l.name),
			entpublishlane.UnitID(unitID),
			entpublishlane.Holder(holder),
			entpublishlane.LeaseUntilGT(now),
		).
		SetLeaseUntil(now.Add(lease)).
		SetUpdatedAt(now).
		Save(ctx)
	if err != nil {
		return false, err
	}
	return updated == 1, nil
}

// Validate is the fencing check performed before and after an irreversible
// action. It does not extend the lease.
func (l *publishLane) Validate(ctx context.Context, unitID, holder string) (bool, error) {
	now := l.now()

	count, err := l.db.PublishLane.Query().
		Where(
			entpublishlane.Lane(l.name),
			entpublishlane.UnitID(unitID),
			entpublishlane.Holder(holder),
			entpublishlane.LeaseUntilGT(now),
		).
		Count(ctx)
	if err != nil {
		return false, err
	}
	return count == 1, nil
}

// Release frees the lane once every step of the unit is terminal. A release by
// a non-holder is a no-op so a stale worker cannot free someone else's lane.
func (l *publishLane) Release(ctx context.Context, unitID string) error {
	now := l.now()

	_, err := l.db.PublishLane.Update().
		Where(
			entpublishlane.Lane(l.name),
			entpublishlane.UnitID(unitID),
		).
		ClearUnitID().
		ClearHolder().
		ClearLeaseUntil().
		SetUpdatedAt(now).
		Save(ctx)
	return err
}

// heartbeat renews the lease until ctx is done. When a renewal fails the lane
// was taken over (or expired), so the context is cancelled: the running
// execution must stop before it touches the mirror again.
func (l *publishLane) heartbeat(ctx context.Context, cancel context.CancelFunc, unitID, holder string, interval, lease time.Duration) {
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			ok, err := l.Renew(ctx, unitID, holder, lease)
			if err != nil || !ok {
				cancel()
				return
			}
		}
	}
}
