package impl

import (
	"context"
	"errors"
	"fmt"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
)

// ErrNothingToResume means the unit has no failed or skipped step.
var ErrNothingToResume = errors.New("publish unit has no failed step to resume")

func (s *publishsrvc) resumeUnit(ctx context.Context, unitID string) (*ent.PublishUnit, error) {
	unit, steps, err := s.loadUnit(ctx, unitID)
	if err != nil {
		return nil, err
	}

	retryable := 0
	for _, step := range steps {
		if step.State == PublishStepStateFailed || step.State == PublishStepStateSkipped {
			retryable++
		}
	}
	if retryable == 0 {
		return nil, fmt.Errorf("%w: unit %s", ErrNothingToResume, unitID)
	}

	tx, err := s.db.Tx(ctx)
	if err != nil {
		return nil, err
	}
	rollback := func(err error) error {
		if rbErr := tx.Rollback(); rbErr != nil {
			return errors.Join(err, rbErr)
		}
		return err
	}

	if _, err := tx.PublishStep.Update().
		Where(
			entpublishstep.UnitID(unitID),
			entpublishstep.StateIn(PublishStepStateFailed, PublishStepStateSkipped),
		).
		SetState(PublishStepStateBlocked).
		ClearClaimedBy().
		ClearLeaseUntil().
		ClearAvailableAt().
		ClearLastError().
		ClearStartedAt().
		ClearFinishedAt().
		Save(ctx); err != nil {
		return nil, rollback(err)
	}

	// Promote the steps whose dependency is already satisfied (or absent).
	pending, err := tx.PublishStep.Query().
		Where(entpublishstep.UnitID(unitID)).
		All(ctx)
	if err != nil {
		return nil, rollback(err)
	}

	byID := make(map[string]*ent.PublishStep, len(pending))
	for _, step := range pending {
		byID[step.ID] = step
	}
	for _, step := range pending {
		if step.State != PublishStepStateBlocked {
			continue
		}
		ready := step.DependsOn == ""
		if !ready {
			if dep, ok := byID[step.DependsOn]; ok && dep.State == PublishStepStateSucceeded {
				ready = true
			}
		}
		if !ready {
			continue
		}
		if _, err := tx.PublishStep.UpdateOneID(step.ID).
			SetState(PublishStepStatePending).
			Save(ctx); err != nil {
			return nil, rollback(err)
		}
	}

	updated, err := tx.PublishUnit.UpdateOneID(unitID).
		SetState(PublishStateQueued).
		ClearFinishedAt().
		Save(ctx)
	if err != nil {
		return nil, rollback(err)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}

	s.logger.Info().
		Str("unit_id", unitID).
		Str("kind", unit.Kind).
		Int("steps", retryable).
		Msg("publish unit resumed")

	return updated, nil
}

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
			SetLastError(fmt.Sprintf("skipped: previous step %s failed", stepID)).
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
