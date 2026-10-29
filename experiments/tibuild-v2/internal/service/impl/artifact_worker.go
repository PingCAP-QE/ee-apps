package impl

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
	workerpkg "github.com/PingCAP-QE/ee-apps/tibuild/internal/worker"
)

const (
	defaultPollingRate = 10 * time.Second
	defaultBatchSize   = 5
)

// Start launches the image sync workers in the background. Each concurrency
// slot runs the shared polling loop; the reaper returns expired leases to
// PENDING. In lease mode nothing is reset on startup, so a restart cannot
// steal another region's in-flight tasks.
func (s *artifactsrvc) Start(ctx context.Context) {
	cfg := s.workerConfig()

	if cfg.LegacyReclaimEnabled() {
		updated, err := s.db.ImageSyncTask.Update().
			Where(entimagesynctask.Status(TaskStatusProcessing)).
			SetStatus(TaskStatusPending).
			ClearClaimedBy().
			ClearLeaseUntil().
			Save(ctx)
		if err != nil {
			s.logger.Err(err).Msg("failed to reclaim interrupted image sync tasks")
		} else {
			s.logger.Info().Int("count", updated).Msg("reclaimed interrupted image sync tasks on startup")
		}
	}

	go s.runReaper(ctx)

	s.mu.RLock()
	pollingRate := s.pollingRate
	s.mu.RUnlock()

	var wg sync.WaitGroup
	for i := 0; i < cfg.Concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			runner := &workerpkg.Runner{
				Logger:       s.logger,
				PollInterval: pollingRate,
				Consume:      s.pollOnce,
			}
			runner.Run(ctx)
		}()
	}
	wg.Wait()
}

// pollOnce drains a small batch of claimable tasks before waiting for the next
// poll interval.
func (s *artifactsrvc) pollOnce(ctx context.Context) {
	for i := 0; i < defaultBatchSize; i++ {
		if ctx.Err() != nil {
			return
		}
		task, err := s.claimOne(ctx)
		if err != nil {
			s.logger.Err(err).Msg("failed to claim an image sync task")
			return
		}
		if task == nil {
			return
		}
		s.process(ctx, task)
	}
}

// process copies the image for a claimed task and records the result with a
// fencing check.
func (s *artifactsrvc) process(ctx context.Context, task *ent.ImageSyncTask) {
	s.inflight.Add(1)
	defer s.inflight.Done()

	l := s.logger.With().
		Int("id", task.ID).
		Str("source", task.Source).
		Str("target", task.Target).
		Logger()

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	copyCtx := runCtx
	hardTimeout := s.workerConfig().HardTimeoutDuration()
	if hardTimeout > 0 {
		timeoutCtx, timeoutCancel := context.WithTimeout(runCtx, hardTimeout)
		defer timeoutCancel()
		copyCtx = timeoutCtx
	}

	// Keep the lease alive for the whole copy. A failed renewal cancels the
	// copy early; the result is then discarded instead of overwriting the
	// next owner's work.
	if task.ClaimedBy != "" {
		go s.heartbeat(runCtx, cancel, task.ID, task.ClaimedBy)
	}

	l.Info().Msg("Syncing Docker image")

	err := crane.Copy(task.Source, task.Target, crane.WithContext(copyCtx))
	if ctx.Err() != nil {
		l.Info().Msg("worker stopping, discarding image sync result")
		return
	}
	if runCtx.Err() != nil && err != nil {
		l.Warn().Msg("task lease lost, discarding image sync result")
		return
	}
	if hardTimeout > 0 && copyCtx.Err() == context.DeadlineExceeded {
		err = fmt.Errorf("image sync exceeded hard timeout %s: %w", hardTimeout, err)
	}

	if err != nil {
		ok, updateErr := s.handleFailure(ctx, task, err)
		if updateErr != nil {
			l.Err(updateErr).Msg("failed to record image sync failure")
			return
		}
		if !ok {
			l.Warn().Msg("task lease lost, failure not recorded")
			return
		}
		if task.Attempts >= s.workerConfig().MaxAttempts {
			l.Err(err).Msg("Failed to sync image, giving up")
		} else {
			l.Warn().Err(err).Int("attempt", task.Attempts).Msg("Failed to sync image, re-enqueuing")
		}
		return
	}

	ok, err := s.markSucceeded(ctx, task)
	if err != nil {
		l.Err(err).Msg("failed to record image sync success")
		return
	}
	if !ok {
		l.Warn().Msg("task lease lost, success not recorded")
		return
	}
	l.Info().Msg("Image successfully synced")
}

// Stop waits for in-flight copies to finish before shutting down.
func (s *artifactsrvc) Stop() {
	s.logger.Info().Msg("waiting for in-flight image sync tasks to finish")
	s.inflight.Wait()
}
