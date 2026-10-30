package impl

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/hex"
	"fmt"
	mathrand "math/rand/v2"
	"time"

	"github.com/google/go-containerregistry/pkg/name"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

const (
	claimRetries    = 5
	claimTokenBytes = 8
)

// now returns the service clock. Tests replace nowFn for deterministic leases.
func (s *artifactsrvc) now() time.Time {
	if s.nowFn != nil {
		return s.nowFn()
	}
	return time.Now()
}

func (s *artifactsrvc) workerConfig() config.Worker {
	s.mu.RLock()
	cfg := s.workerCfg
	s.mu.RUnlock()
	cfg.ApplyDefaults()
	return cfg
}

// claimOne atomically claims the oldest claimable task for this worker.
//
// The candidate is selected first and then updated with a status guard. Two
// workers may select the same row; only one UPDATE observes status=PENDING and
// increments attempts, the loser retries with the next candidate.
func (s *artifactsrvc) claimOne(ctx context.Context) (*ent.ImageSyncTask, error) {
	cfg := s.workerConfig()

	for i := 0; i < claimRetries; i++ {
		now := s.now()
		query := s.db.ImageSyncTask.Query().
			Where(entimagesynctask.Status(TaskStatusPending)).
			Where(entimagesynctask.Or(
				entimagesynctask.AvailableAtIsNil(),
				entimagesynctask.AvailableAtLTE(now),
			)).
			Order(entimagesynctask.ByID())
		if !hasWildcardTarget(cfg.Targets) {
			query = query.Where(entimagesynctask.TargetKeyIn(cfg.Targets...))
		}

		candidate, err := query.First(ctx)
		if ent.IsNotFound(err) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}

		token := s.newClaimToken()
		updated, err := s.db.ImageSyncTask.Update().
			Where(
				entimagesynctask.ID(candidate.ID),
				entimagesynctask.Status(TaskStatusPending),
				entimagesynctask.Or(
					entimagesynctask.AvailableAtIsNil(),
					entimagesynctask.AvailableAtLTE(now),
				),
			).
			SetStatus(TaskStatusProcessing).
			SetClaimedBy(token).
			SetLeaseUntil(now.Add(cfg.LeaseDuration())).
			AddAttempts(1).
			SetUpdatedAt(now).
			Save(ctx)
		if err != nil {
			return nil, err
		}
		if updated == 1 {
			return s.db.ImageSyncTask.Query().
				Where(entimagesynctask.ID(candidate.ID), entimagesynctask.ClaimedBy(token)).
				Only(ctx)
		}
	}

	return nil, nil
}

// renewLease extends the lease only when the caller still owns the task.
func (s *artifactsrvc) renewLease(ctx context.Context, id int, token string, lease time.Duration) (bool, error) {
	updated, err := s.db.ImageSyncTask.Update().
		Where(
			entimagesynctask.ID(id),
			entimagesynctask.ClaimedBy(token),
			entimagesynctask.Status(TaskStatusProcessing),
		).
		SetLeaseUntil(s.now().Add(lease)).
		Save(ctx)
	return updated == 1, err
}

// heartbeat renews the lease until the process finishes. A failed renewal
// means the lease was lost; cancel the running copy so it stops early.
func (s *artifactsrvc) heartbeat(ctx context.Context, cancel context.CancelFunc, id int, token string) {
	cfg := s.workerConfig()
	ticker := time.NewTicker(cfg.HeartbeatDuration())
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			ok, err := s.renewLease(ctx, id, token, cfg.LeaseDuration())
			if err != nil {
				s.logger.Warn().Err(err).Int("id", id).Msg("failed to renew task lease")
				continue
			}
			if !ok {
				s.logger.Warn().Int("id", id).Msg("task lease lost, aborting")
				cancel()
				return
			}
		}
	}
}

// reapOnce returns expired PROCESSING tasks to PENDING.
func (s *artifactsrvc) reapOnce(ctx context.Context) (int, error) {
	now := s.now()
	return s.db.ImageSyncTask.Update().
		Where(
			entimagesynctask.Status(TaskStatusProcessing),
			entimagesynctask.LeaseUntilNotNil(),
			entimagesynctask.LeaseUntilLT(now),
		).
		SetStatus(TaskStatusPending).
		ClearClaimedBy().
		ClearLeaseUntil().
		SetUpdatedAt(now).
		Save(ctx)
}

func (s *artifactsrvc) runReaper(ctx context.Context) {
	ticker := time.NewTicker(s.workerConfig().ReapEvery())
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			n, err := s.reapAndRecord(ctx)
			if err != nil {
				s.logger.Err(err).Msg("failed to reap expired task leases")
				continue
			}
			if n > 0 {
				s.logger.Warn().Int("count", n).Msg("reclaimed expired task leases")
			}
		}
	}
}

// reapAndRecord reclaims expired leases and records the metrics/alert state.
func (s *artifactsrvc) reapAndRecord(ctx context.Context) (int, error) {
	n, err := s.reapOnce(ctx)
	if err != nil || n == 0 {
		return n, err
	}
	if s.metrics != nil {
		s.metrics.recordReclaimed(n)
	}
	// Publish the reclaim so the alert leader can report it even when this
	// worker is a follower; only the leader calls the notifier.
	if err := s.recordLeaseReclaim(ctx, n, s.now()); err != nil {
		s.logger.Err(err).Msg("failed to record the lease reclaim alert event")
	}
	return n, nil
}

// markSucceeded writes the result only while the caller still holds the lease.
func (s *artifactsrvc) markSucceeded(ctx context.Context, task *ent.ImageSyncTask) (bool, error) {
	if task == nil || task.ClaimedBy == "" {
		return false, nil
	}
	updated, err := s.db.ImageSyncTask.Update().
		Where(
			entimagesynctask.ID(task.ID),
			entimagesynctask.ClaimedBy(task.ClaimedBy),
			entimagesynctask.Status(TaskStatusProcessing),
		).
		SetStatus(TaskStatusSucceeded).
		SetErrMsg("").
		SetRetryCount(task.Attempts).
		ClearClaimedBy().
		ClearLeaseUntil().
		SetUpdatedAt(s.now()).
		Save(ctx)
	return updated == 1, err
}

// handleFailure either schedules a backoff retry or marks the task FAILED.
// Both paths are fenced by claimed_by: an expired owner cannot overwrite a
// task that has already been reclaimed.
func (s *artifactsrvc) handleFailure(ctx context.Context, task *ent.ImageSyncTask, cause error) (bool, error) {
	if task == nil || task.ClaimedBy == "" {
		return false, nil
	}
	cfg := s.workerConfig()
	now := s.now()
	update := s.db.ImageSyncTask.Update().
		Where(
			entimagesynctask.ID(task.ID),
			entimagesynctask.ClaimedBy(task.ClaimedBy),
			entimagesynctask.Status(TaskStatusProcessing),
		).
		SetErrMsg(truncateErrorMessage(cause.Error())).
		SetRetryCount(task.Attempts).
		SetUpdatedAt(now)

	if task.Attempts >= cfg.MaxAttempts {
		update = update.SetStatus(TaskStatusFailed).ClearClaimedBy().ClearLeaseUntil()
	} else {
		update = update.
			SetStatus(TaskStatusPending).
			SetAvailableAt(now.Add(s.backoff(task.Attempts))).
			ClearClaimedBy().
			ClearLeaseUntil()
	}

	updated, err := update.Save(ctx)
	if err == nil && updated == 1 && task.Attempts >= cfg.MaxAttempts && s.metrics != nil {
		target := task.TargetKey
		if target == "" {
			target = "unknown"
		}
		s.metrics.recordFailed(target, "max_attempts")
	}
	return updated == 1, err
}

// backoff grows exponentially from worker.backoff_base, capped by
// worker.backoff_max, with +/-20% jitter.
func (s *artifactsrvc) backoff(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	cfg := s.workerConfig()
	base := cfg.BackoffBaseDuration()
	maxDelay := cfg.BackoffMaxDuration()
	d := base
	for i := 1; i < attempts && d < maxDelay; i++ {
		d *= 2
	}
	if d <= 0 || d > maxDelay {
		d = maxDelay
	}
	jitter := d / 5
	if jitter <= 0 {
		return d
	}
	return d - jitter + time.Duration(mathrand.Int64N(int64(2*jitter)+1))
}

func truncateErrorMessage(message string) string {
	const maxLen = 512
	runes := []rune(message)
	if len(runes) <= maxLen {
		return message
	}
	return string(runes[:maxLen])
}

func (s *artifactsrvc) newClaimToken() string {
	buf := make([]byte, claimTokenBytes)
	if _, err := cryptorand.Read(buf); err != nil {
		return fmt.Sprintf("%s-%d", s.workerConfig().ID, time.Now().UnixNano())
	}
	return fmt.Sprintf("%s-%s", s.workerConfig().ID, hex.EncodeToString(buf))
}

func hasWildcardTarget(targets []string) bool {
	for _, target := range targets {
		if target == "*" {
			return true
		}
	}
	return false
}

// imageTargetKey extracts the registry host used for worker sharding.
func imageTargetKey(image string) string {
	ref, err := name.ParseReference(image)
	if err != nil {
		return ""
	}
	registry := ref.Context().RegistryStr()
	if registry == name.DefaultRegistry {
		return "docker.io"
	}
	return registry
}
