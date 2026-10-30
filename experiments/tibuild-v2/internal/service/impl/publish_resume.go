package impl

import (
	"context"

	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
)

// promoteRunnableSteps releases the blocked steps whose dependency is already
// satisfied (or absent). Ordered units call it after a step succeeds and after
// a retry re-blocks the failed steps, so the order is always respected.
func (s *publishsrvc) promoteRunnableSteps(ctx context.Context, unitID string) error {
	steps, err := s.db.PublishStep.Query().
		Where(entpublishstep.UnitID(unitID)).
		All(ctx)
	if err != nil {
		return err
	}

	byID := make(map[string]string, len(steps))
	for _, step := range steps {
		byID[step.ID] = step.State
	}

	for _, step := range steps {
		if step.State != PublishStepStateBlocked {
			continue
		}
		ready := step.DependsOn == ""
		if !ready {
			ready = byID[step.DependsOn] == PublishStepStateSucceeded
		}
		if !ready {
			continue
		}
		if _, err := s.db.PublishStep.UpdateOneID(step.ID).
			SetState(PublishStepStatePending).
			Save(ctx); err != nil {
			return err
		}
	}
	return nil
}

// promoteDependents makes the steps waiting on stepID claimable.
func (s *publishsrvc) promoteDependents(ctx context.Context, stepID string) error {
	_, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.DependsOn(stepID),
			entpublishstep.State(PublishStepStateBlocked),
		).
		SetState(PublishStepStatePending).
		Save(ctx)
	return err
}

// skipDependents marks the steps waiting on a failed step as skipped,
// recursively, so no step is left blocked forever.
func (s *publishsrvc) skipDependents(ctx context.Context, stepID string) error {
	dependents, err := s.db.PublishStep.Query().
		Where(
			entpublishstep.DependsOn(stepID),
			entpublishstep.State(PublishStepStateBlocked),
		).
		All(ctx)
	if err != nil {
		return err
	}

	now := s.now()
	for _, step := range dependents {
		updated, err := s.db.PublishStep.Update().
			Where(
				entpublishstep.ID(step.ID),
				entpublishstep.State(PublishStepStateBlocked),
			).
			SetState(PublishStepStateSkipped).
			SetLastError("skipped: the previous step failed").
			SetFinishedAt(now).
			Save(ctx)
		if err != nil {
			return err
		}
		if updated == 1 {
			if err := s.skipDependents(ctx, step.ID); err != nil {
				return err
			}
		}
	}
	return nil
}
