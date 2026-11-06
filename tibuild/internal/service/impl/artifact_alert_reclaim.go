package impl

import (
	"context"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entalertevent "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/alertevent"
)

// leaseReclaimEventName is the alert_events row that carries the last lease
// reclaim. The reaper runs on every worker, but only the elected leader sends
// alerts, so the timestamp must be shared instead of kept in process memory.
const leaseReclaimEventName = "lease-reclaim"

// recordLeaseReclaim publishes a reaper result to every worker. A follower that
// reclaims a task makes the leader alert on the next tick.
func (s *artifactsrvc) recordLeaseReclaim(ctx context.Context, n int, now time.Time) error {
	if n <= 0 {
		return nil
	}
	updated, err := s.db.AlertEvent.Update().
		Where(entalertevent.Name(leaseReclaimEventName)).
		SetLastAt(now).
		AddCount(n).
		Save(ctx)
	if err != nil {
		return err
	}
	if updated == 1 {
		return nil
	}

	_, err = s.db.AlertEvent.Create().
		SetName(leaseReclaimEventName).
		SetLastAt(now).
		SetCount(n).
		Save(ctx)
	if err == nil {
		return nil
	}
	if !ent.IsConstraintError(err) {
		return err
	}
	// Another worker created the row first; fold this reclaim into it.
	_, err = s.db.AlertEvent.Update().
		Where(entalertevent.Name(leaseReclaimEventName)).
		SetLastAt(now).
		AddCount(n).
		Save(ctx)
	return err
}

// lastLeaseReclaim returns the last reclaim recorded by any worker. A zero time
// means no reclaim has happened yet.
func (s *artifactsrvc) lastLeaseReclaim(ctx context.Context) (time.Time, error) {
	event, err := s.db.AlertEvent.Query().
		Where(entalertevent.Name(leaseReclaimEventName)).
		Only(ctx)
	if ent.IsNotFound(err) {
		return time.Time{}, nil
	}
	if err != nil {
		return time.Time{}, err
	}
	return event.LastAt, nil
}
