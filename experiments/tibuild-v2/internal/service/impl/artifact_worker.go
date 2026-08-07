package impl

import (
	"context"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
)

const (
	defaultPollingRate = 10 * time.Second
	defaultBatchSize   = 5
	defaultMaxRetries  = 3
)

// Start launches the image sync worker in the background. It polls the
// database for PENDING tasks and copies the images, so tasks survive a
// service restart (residual PROCESSING tasks are reclaimed on startup).
// It blocks until the context is cancelled.
func (s *artifactsrvc) Start(ctx context.Context) {
	// Reclaim tasks that were left in PROCESSING by a previous run.
	if _, err := s.db.ImageSyncTask.Update().
		Where(entimagesynctask.Status(TaskStatusProcessing)).
		SetStatus(TaskStatusPending).
		Save(ctx); err != nil {
		s.logger.Err(err).Msg("failed to reclaim interrupted image sync tasks")
	} else {
		s.logger.Info().Msg("reclaimed interrupted image sync tasks on startup")
	}

	s.mu.RLock()
	pollingRate := s.pollingRate
	s.mu.RUnlock()

	ticker := time.NewTicker(pollingRate)
	defer ticker.Stop()

	s.logger.Info().Dur("polling_rate", pollingRate).Msg("image sync worker started")
	for {
		select {
		case <-ctx.Done():
			s.logger.Info().Msg("image sync worker stopped")
			return
		case <-ticker.C:
			s.pollOnce(ctx)
		}
	}
}

// pollOnce claims and processes a batch of PENDING tasks.
func (s *artifactsrvc) pollOnce(ctx context.Context) {
	tasks, err := s.db.ImageSyncTask.Query().
		Where(entimagesynctask.Status(TaskStatusPending)).
		Order(entimagesynctask.ByID()).
		Limit(defaultBatchSize).
		All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to query pending image sync tasks")
		return
	}

	for _, task := range tasks {
		// Claim the task optimistically: only a task still in PENDING state is
		// taken, which guards against concurrent workers picking it up twice.
		claimed, err := s.db.ImageSyncTask.UpdateOneID(task.ID).
			Where(entimagesynctask.Status(TaskStatusPending)).
			SetStatus(TaskStatusProcessing).
			SetUpdatedAt(time.Now()).
			Save(ctx)
		if err != nil || claimed == nil {
			continue
		}
		s.process(ctx, claimed)
	}
}

// process copies the image for a claimed task and updates its status.
func (s *artifactsrvc) process(ctx context.Context, task *ent.ImageSyncTask) {
	s.inflight.Add(1)
	defer s.inflight.Done()

	l := s.logger.With().
		Int("id", task.ID).
		Str("source", task.Source).
		Str("target", task.Target).
		Logger()

	l.Info().Msg("Syncing Docker image")

	err := crane.Copy(task.Source, task.Target, crane.WithContext(ctx))
	if err != nil {
		retryCount := task.RetryCount + 1
		if retryCount < defaultMaxRetries {
			l.Warn().Err(err).Int("retry_count", retryCount).Msg("Failed to sync image, re-enqueuing")
			_, _ = s.db.ImageSyncTask.UpdateOneID(task.ID).
				SetStatus(TaskStatusPending).
				SetRetryCount(retryCount).
				SetErrMsg(err.Error()).
				SetUpdatedAt(time.Now()).
				Save(context.Background())
			return
		}
		l.Err(err).Msg("Failed to sync image, giving up")
		_, _ = s.db.ImageSyncTask.UpdateOneID(task.ID).
			SetStatus(TaskStatusFailed).
			SetRetryCount(retryCount).
			SetErrMsg(err.Error()).
			SetUpdatedAt(time.Now()).
			Save(context.Background())
		return
	}

	l.Info().Msg("Image successfully synced to DockerHub")
	_, _ = s.db.ImageSyncTask.UpdateOneID(task.ID).
		SetStatus(TaskStatusSucceeded).
		SetErrMsg("").
		SetUpdatedAt(time.Now()).
		Save(context.Background())
}

// Stop waits for in-flight copies to finish before shutting down.
func (s *artifactsrvc) Stop() {
	s.logger.Info().Msg("waiting for in-flight image sync tasks to finish")
	s.inflight.Wait()
}
