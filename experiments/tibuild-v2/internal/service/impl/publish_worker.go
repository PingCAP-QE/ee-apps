package impl

import (
	"context"
	"fmt"
	"math/rand/v2"
	"net/http"
	"sync"
	"time"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	entpublishunit "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishunit"
	workerpkg "github.com/PingCAP-QE/ee-apps/tibuild/internal/worker"
)

const (
	publishClaimRetries   = 5
	publishClaimBatchSize = 5
	publishReapInterval   = 30 * time.Second
	publishBackoffBase    = 10 * time.Second
	publishBackoffMax     = 5 * time.Minute
)

// imageCopyResult is the outcome recorded on a succeeded step.
type imageCopyResult struct {
	Digest  string   `json:"digest"`
	Dest    string   `json:"dest"`
	Copied  []string `json:"copied"`
	Skipped []string `json:"skipped"`
}

// Start launches the publish step executor. It returns when ctx is cancelled
// and every in-flight step has finished.
//
// Steps are claimed per target: a worker only takes work for the registries it
// declares (worker.targets), so the destinations of one unit are executed in
// parallel by the regional workers that can reach them. There is no lane: image
// targets have no ordering between each other.
func (s *publishsrvc) Start(ctx context.Context) {
	cfg := s.workerCfg
	if !containsString(cfg.Kinds, PublishKindImageCopy) {
		s.logger.Info().Strs("kinds", cfg.Kinds).Msg("publish worker disabled for the configured kinds")
		return
	}

	go s.runPublishReaper(ctx)

	var wg sync.WaitGroup
	for i := 0; i < cfg.Concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			runner := &workerpkg.Runner{
				Logger:       s.logger,
				PollInterval: cfg.PollEvery(),
				Consume:      s.consumePublishSteps,
			}
			runner.Run(ctx)
		}()
	}
	wg.Wait()
}

// Stop waits for in-flight step executions to finish.
func (s *publishsrvc) Stop() {
	s.inflight.Wait()
}

// MetricsHandler returns nil: the worker metrics are exposed by the artifact
// worker, which serves /metrics for the process.
func (s *publishsrvc) MetricsHandler() http.Handler {
	return nil
}

// consumePublishSteps drains a small batch of claimable steps per poll.
func (s *publishsrvc) consumePublishSteps(ctx context.Context) {
	for i := 0; i < publishClaimBatchSize; i++ {
		if ctx.Err() != nil {
			return
		}
		step, err := s.claimStep(ctx)
		if err != nil {
			s.logger.Err(err).Msg("failed to claim a publish step")
			return
		}
		if step == nil {
			return
		}
		s.executeStep(ctx, step)
	}
}

// claimStep atomically claims the oldest pending step this worker serves.
//
// The candidate is selected first and then updated with a state guard: two
// workers may select the same row, but only one UPDATE observes state=pending
// and increments attempts; the loser moves on to the next candidate.
func (s *publishsrvc) claimStep(ctx context.Context) (*ent.PublishStep, error) {
	cfg := s.workerCfg

	for i := 0; i < publishClaimRetries; i++ {
		now := s.now()
		query := s.db.PublishStep.Query().
			Where(entpublishstep.State(PublishStepStatePending)).
			Where(entpublishstep.Or(
				entpublishstep.AvailableAtIsNil(),
				entpublishstep.AvailableAtLTE(now),
			)).
			Order(entpublishstep.ByCreatedAt(), entpublishstep.ByID())
		if !hasWildcardTarget(cfg.Targets) {
			query = query.Where(entpublishstep.TargetIn(cfg.Targets...))
		}

		candidate, err := query.First(ctx)
		if ent.IsNotFound(err) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}

		token := s.claimToken()
		update := s.db.PublishStep.Update().
			Where(
				entpublishstep.ID(candidate.ID),
				entpublishstep.State(PublishStepStatePending),
				entpublishstep.Or(
					entpublishstep.AvailableAtIsNil(),
					entpublishstep.AvailableAtLTE(now),
				),
			).
			SetState(PublishStepStateClaimed).
			SetClaimedBy(token).
			SetLeaseUntil(now.Add(cfg.LeaseDuration())).
			AddAttempts(1)
		if candidate.StartedAt == nil {
			update = update.SetStartedAt(now)
		}
		updated, err := update.Save(ctx)
		if err != nil {
			return nil, err
		}
		if updated == 1 {
			// The unit leaves the queue as soon as one of its steps runs.
			if err := s.markUnitRunning(ctx, candidate.UnitID); err != nil {
				s.logger.Err(err).Str("unit_id", candidate.UnitID).Msg("failed to mark the publish unit as running")
			}
			return s.db.PublishStep.Query().
				Where(entpublishstep.ID(candidate.ID), entpublishstep.ClaimedBy(token)).
				Only(ctx)
		}
	}
	return nil, nil
}

func (s *publishsrvc) claimToken() string {
	return fmt.Sprintf("%s-%s", s.workerCfg.ID, newUUID())
}

func (s *publishsrvc) markUnitRunning(ctx context.Context, unitID string) error {
	err := s.db.PublishUnit.UpdateOneID(unitID).
		Where(entpublishunit.State(PublishStateQueued)).
		SetState(PublishStateRunning).
		Exec(ctx)
	if ent.IsNotFound(err) {
		// Already running (or finished); nothing to do.
		return nil
	}
	return err
}

// executeStep runs the claimed step and records its outcome. Result writes are
// fenced by claimed_by, so a worker that lost its lease cannot overwrite the
// work of the worker that took over.
func (s *publishsrvc) executeStep(ctx context.Context, step *ent.PublishStep) {
	s.inflight.Add(1)
	defer s.inflight.Done()

	log := s.logger.With().
		Str("step_id", step.ID).
		Str("unit_id", step.UnitID).
		Str("target", step.Target).
		Str("dest", step.Dest).
		Logger()

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	if hardTimeout := s.workerCfg.HardTimeoutDuration(); hardTimeout > 0 {
		timeoutCtx, timeoutCancel := context.WithTimeout(runCtx, hardTimeout)
		defer timeoutCancel()
		runCtx = timeoutCtx
	}

	// Keep the lease alive for the whole copy. A failed renewal cancels the
	// copy early; the result is then discarded instead of overwriting the next
	// owner's work.
	go s.heartbeatStep(runCtx, cancel, step.ID, step.ClaimedBy)

	payload, err := decodeImageCopyPayload(step.Payload)
	if err != nil {
		s.recordStepFailure(ctx, step, fmt.Errorf("invalid step payload: %w", err), &log)
		return
	}

	log.Info().Str("source", payload.Source).Strs("tags", payload.Tags).Msg("copying the image")

	result, err := s.copyImage(runCtx, payload)
	if ctx.Err() != nil {
		log.Info().Msg("worker stopping, discarding the step result")
		return
	}
	if runCtx.Err() != nil && err != nil {
		log.Warn().Msg("step lease lost, discarding the step result")
		return
	}
	if err != nil {
		s.recordStepFailure(ctx, step, err, &log)
		return
	}

	stored, err := payloadMap(result)
	if err != nil {
		log.Err(err).Msg("failed to encode the step result")
		return
	}
	ok, err := s.markStepSucceeded(ctx, step, stored)
	if err != nil {
		log.Err(err).Msg("failed to record the step result")
		return
	}
	if !ok {
		log.Warn().Msg("step lease lost, success not recorded")
		return
	}
	s.refreshUnit(ctx, step.UnitID)
	log.Info().
		Int("copied", len(result.Copied)).
		Int("skipped", len(result.Skipped)).
		Msg("image copied")
}

// copyImage writes every tag of the payload on the destination repository.
//
// Tags are processed in order: a step carries at most a couple of them (the
// source tag, a constant tag such as `nightly` and the tag derived by
// tag_regex_replace), so per-tag parallelism would not pay for itself.
func (s *publishsrvc) copyImage(ctx context.Context, payload ImageCopyPayload) (imageCopyResult, error) {
	result := imageCopyResult{
		Digest:  payload.Digest,
		Dest:    payload.Dest,
		Copied:  make([]string, 0, len(payload.Tags)),
		Skipped: make([]string, 0, len(payload.Tags)),
	}

	for _, tag := range payload.Tags {
		if err := ctx.Err(); err != nil {
			return imageCopyResult{}, err
		}
		dst := payload.Dest + ":" + tag

		// Idempotency: the destination already holds the planned digest, so
		// there is nothing to send.
		if current, err := s.images.Digest(ctx, dst); err == nil && current == payload.Digest {
			result.Skipped = append(result.Skipped, dst)
			continue
		}

		if err := s.images.Copy(ctx, payload.Source, dst); err != nil {
			return imageCopyResult{}, fmt.Errorf("copy %s to %s: %w", payload.Source, dst, err)
		}

		// Verify that the destination really serves the planned digest: pinning
		// it is only worth something if the targets agree at the end.
		if current, err := s.images.Digest(ctx, dst); err == nil && current != payload.Digest {
			return imageCopyResult{}, fmt.Errorf("copy %s to %s: destination serves %s, want %s", payload.Source, dst, current, payload.Digest)
		}
		result.Copied = append(result.Copied, dst)
	}

	return result, nil
}

// heartbeatStep renews the lease until the execution finishes. A failed
// renewal means the lease was lost: cancel the copy so it stops early.
func (s *publishsrvc) heartbeatStep(ctx context.Context, cancel context.CancelFunc, id, token string) {
	interval := s.workerCfg.HeartbeatDuration()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			ok, err := s.renewStepLease(ctx, id, token)
			if err != nil {
				s.logger.Warn().Err(err).Str("step_id", id).Msg("failed to renew the step lease")
				continue
			}
			if !ok {
				s.logger.Warn().Str("step_id", id).Msg("step lease lost, aborting")
				cancel()
				return
			}
		}
	}
}

func (s *publishsrvc) renewStepLease(ctx context.Context, id, token string) (bool, error) {
	updated, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.ID(id),
			entpublishstep.ClaimedBy(token),
			entpublishstep.State(PublishStepStateClaimed),
		).
		SetLeaseUntil(s.now().Add(s.workerCfg.LeaseDuration())).
		Save(ctx)
	return updated == 1, err
}

// markStepSucceeded writes the result only while the caller still holds the
// lease.
func (s *publishsrvc) markStepSucceeded(ctx context.Context, step *ent.PublishStep, result map[string]any) (bool, error) {
	if step == nil || step.ClaimedBy == "" {
		return false, nil
	}
	updated, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.ID(step.ID),
			entpublishstep.ClaimedBy(step.ClaimedBy),
			entpublishstep.State(PublishStepStateClaimed),
		).
		SetState(PublishStepStateSucceeded).
		SetResult(result).
		ClearLastError().
		ClearClaimedBy().
		ClearLeaseUntil().
		SetFinishedAt(s.now()).
		Save(ctx)
	return updated == 1, err
}

// handleStepFailure either schedules a backoff retry or marks the step failed.
// Both paths are fenced by claimed_by.
func (s *publishsrvc) handleStepFailure(ctx context.Context, step *ent.PublishStep, cause error) (bool, error) {
	if step == nil || step.ClaimedBy == "" {
		return false, nil
	}
	now := s.now()
	update := s.db.PublishStep.Update().
		Where(
			entpublishstep.ID(step.ID),
			entpublishstep.ClaimedBy(step.ClaimedBy),
			entpublishstep.State(PublishStepStateClaimed),
		).
		SetLastError(truncateErrorMessage(cause.Error()))

	if step.Attempts >= s.workerCfg.MaxAttempts {
		update = update.
			SetState(PublishStepStateFailed).
			SetFinishedAt(now).
			ClearClaimedBy().
			ClearLeaseUntil()
	} else {
		update = update.
			SetState(PublishStepStatePending).
			SetAvailableAt(now.Add(s.publishBackoff(step.Attempts))).
			ClearClaimedBy().
			ClearLeaseUntil()
	}

	updated, err := update.Save(ctx)
	return updated == 1, err
}

func (s *publishsrvc) recordStepFailure(ctx context.Context, step *ent.PublishStep, cause error, log *zerolog.Logger) {
	ok, err := s.handleStepFailure(ctx, step, cause)
	if err != nil {
		log.Err(err).Msg("failed to record the step failure")
		return
	}
	if !ok {
		log.Warn().Msg("step lease lost, failure not recorded")
		return
	}
	if step.Attempts >= s.workerCfg.MaxAttempts {
		log.Err(cause).Msg("step failed, giving up")
	} else {
		log.Warn().Err(cause).Int("attempt", step.Attempts).Msg("step failed, re-queued with backoff")
	}
	s.refreshUnit(ctx, step.UnitID)
}

// publishBackoff grows exponentially and is capped, with +/-20% jitter.
func (s *publishsrvc) publishBackoff(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	d := publishBackoffBase
	for i := 1; i < attempts && d < publishBackoffMax; i++ {
		d *= 2
	}
	if d <= 0 || d > publishBackoffMax {
		d = publishBackoffMax
	}
	jitter := d / 5
	if jitter <= 0 {
		return d
	}
	return d - jitter + time.Duration(rand.Int64N(int64(2*jitter)+1))
}

// refreshUnit recomputes the unit state from its steps.
//
// The steps are the source of truth: a concurrent update may briefly write a
// slightly stale aggregate, and the next step that finishes corrects it.
func (s *publishsrvc) refreshUnit(ctx context.Context, unitID string) {
	steps, err := s.loadSteps(ctx, unitID)
	if err != nil {
		s.logger.Err(err).Str("unit_id", unitID).Msg("failed to aggregate the publish unit")
		return
	}

	state := aggregateStepStates(steps)
	update := s.db.PublishUnit.UpdateOneID(unitID).SetState(state)
	if isTerminalUnitState(state) {
		update = update.SetFinishedAt(s.now())
	} else {
		update = update.ClearFinishedAt()
	}
	if err := update.Exec(ctx); err != nil && !ent.IsNotFound(err) {
		s.logger.Err(err).Str("unit_id", unitID).Str("state", state).Msg("failed to update the publish unit state")
	}
}

// aggregateStepStates folds the step states into a unit state.
func aggregateStepStates(steps []*ent.PublishStep) string {
	var succeeded, failed, active, claimed int
	for _, step := range steps {
		switch step.State {
		case PublishStepStateSucceeded:
			succeeded++
		case PublishStepStateFailed, PublishStepStateSkipped:
			failed++
		case PublishStepStateClaimed:
			claimed++
			active++
		default:
			active++
		}
	}

	switch {
	case active > 0:
		if claimed > 0 {
			return PublishStateRunning
		}
		return PublishStateQueued
	case succeeded == 0:
		return PublishStateFailed
	case failed == 0:
		return PublishStateSucceeded
	default:
		return PublishStatePartial
	}
}

func isTerminalUnitState(state string) bool {
	switch state {
	case PublishStateSucceeded, PublishStatePartial, PublishStateFailed, PublishStateCanceled:
		return true
	default:
		return false
	}
}

// runPublishReaper returns expired claims to pending. A worker that dies
// mid-copy must not strand its step.
func (s *publishsrvc) runPublishReaper(ctx context.Context) {
	ticker := time.NewTicker(publishReapInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			n, err := s.reapExpiredSteps(ctx)
			if err != nil {
				s.logger.Err(err).Msg("failed to reap expired publish step leases")
				continue
			}
			if n > 0 {
				s.logger.Warn().Int("count", n).Msg("reclaimed expired publish step leases")
			}
		}
	}
}

func (s *publishsrvc) reapExpiredSteps(ctx context.Context) (int, error) {
	now := s.now()
	expired, err := s.db.PublishStep.Query().
		Where(
			entpublishstep.State(PublishStepStateClaimed),
			entpublishstep.LeaseUntilNotNil(),
			entpublishstep.LeaseUntilLT(now),
		).
		All(ctx)
	if err != nil || len(expired) == 0 {
		return 0, err
	}

	updated, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.State(PublishStepStateClaimed),
			entpublishstep.LeaseUntilNotNil(),
			entpublishstep.LeaseUntilLT(now),
		).
		SetState(PublishStepStatePending).
		ClearClaimedBy().
		ClearLeaseUntil().
		Save(ctx)
	if err != nil {
		return 0, err
	}

	units := map[string]struct{}{}
	for _, step := range expired {
		units[step.UnitID] = struct{}{}
	}
	for unitID := range units {
		s.refreshUnit(ctx, unitID)
	}
	return updated, nil
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
