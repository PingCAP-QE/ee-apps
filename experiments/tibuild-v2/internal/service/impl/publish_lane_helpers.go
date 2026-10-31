package impl

import (
	"context"
	"time"
)

func (s *publishsrvc) laneLease() time.Duration {
	heartbeat := s.workerConfig().HeartbeatDuration()
	lease := s.workerConfig().LeaseDuration()
	// A step that hit its hard timeout must not outlive the lane, otherwise a
	// second unit could start writing the same mirror.
	if hard := s.workerConfig().HardTimeoutDuration(); hard > 0 {
		if want := hard + 2*heartbeat; want > lease {
			lease = want
		}
	}
	// Never shorter than a few heartbeats: a single delayed renewal must not
	// drop a healthy worker's lane.
	if floor := 3 * heartbeat; lease < floor {
		lease = floor
	}
	return lease
}

func (s *publishsrvc) acquireLane(ctx context.Context, unitID, workerID string) (bool, error) {
	return s.lane.Acquire(ctx, unitID, workerID, s.laneLease())
}

func (s *publishsrvc) validateLane(ctx context.Context, unitID, workerID string) (bool, error) {
	return s.lane.Validate(ctx, unitID, workerID)
}
