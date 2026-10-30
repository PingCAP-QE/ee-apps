package impl

import (
	"context"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entalertlease "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/alertlease"
)

// alertLeaseName is the single global row that gates alert evaluation. All
// worker replicas in all clusters contend for it, so a condition that is
// visible from several workers still produces one message.
const alertLeaseName = "publish-alerts"

// acquireAlertLease reports whether this worker is the alert leader for the
// current tick and renews the lease as a side effect.
//
// The lease lives in the same database as the task queue, which makes the
// election global without a new coordination service. The TTL is twice
// worker.metrics_interval: a healthy leader renews on every tick, and an
// evicted leader loses the election within one interval.
func (s *artifactsrvc) acquireAlertLease(ctx context.Context, now time.Time) (bool, error) {
	cfg := s.workerConfig()
	holder := cfg.ID
	until := now.Add(2 * cfg.MetricsEvery())

	// Renew our own lease, or take over one that has expired. Both cases are a
	// single guarded UPDATE, so concurrent workers cannot both win.
	updated, err := s.db.AlertLease.Update().
		Where(
			entalertlease.Name(alertLeaseName),
			entalertlease.Or(
				entalertlease.Holder(holder),
				entalertlease.LeaseUntilLT(now),
			),
		).
		SetHolder(holder).
		SetLeaseUntil(until).
		Save(ctx)
	if err != nil {
		return false, err
	}
	if updated == 1 {
		return true, nil
	}

	// No lease row yet: create it. A concurrent creator loses on the unique
	// index and simply waits for the next tick.
	exists, err := s.db.AlertLease.Query().
		Where(entalertlease.Name(alertLeaseName)).
		Exist(ctx)
	if err != nil {
		return false, err
	}
	if exists {
		return false, nil
	}

	_, err = s.db.AlertLease.Create().
		SetName(alertLeaseName).
		SetHolder(holder).
		SetLeaseUntil(until).
		Save(ctx)
	if err != nil {
		if ent.IsConstraintError(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}
