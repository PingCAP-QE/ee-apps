package impl

import (
	"context"
	"time"

	jenkins "github.com/bndr/gojenkins"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

func (s *devbuildsrvc) triggerJenkinsBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// 1. Trigger the Jenkins build
	s.logger.Debug().Str("job", s.jenkins.jobName).Msg("triggering jenkins build")
	queueID, err := s.jenkins.client.BuildJob(ctx, s.jenkins.jobName, nil)
	if err != nil {
		s.logger.Err(err).Msg("failed to trigger jenkins build")
		return nil, err
	}

	// 2. Update record to processing status
	record, err = record.Update().
		SetStatus("processing").
		SetUpdatedAt(time.Now()).
		Save(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to update record status to processing")
		return nil, err
	}

	// 3. Start a goroutine to monitor the build status
	// TODO: push it into async task queue with [asynq](https://github.com/hibiken/asynq)
	go s.monitorBuildStatus(context.Background(), queueID, record.ID)

	return record, nil
}

// monitorBuildStatus watches a Jenkins build until completion and updates the database record
func (s *devbuildsrvc) monitorBuildStatus(ctx context.Context, queueID int64, recordID int) {
	// Get build from queue
	build, err := s.jenkins.client.GetBuildFromQueueID(ctx, queueID)
	if err != nil {
		s.logger.Err(err).Int64("queue_id", queueID).Msg("failed to get build from queue")
		return
	}

	// Wait for build to finish
	for build.IsRunning(ctx) {
		time.Sleep(5 * time.Second)
	}

	// Map Jenkins result to our status
	statusMap := map[string]string{
		jenkins.STATUS_SUCCESS: "success",
		jenkins.STATUS_FAIL:    "failure",
		jenkins.STATUS_ABORTED: "aborted",
		jenkins.STATUS_ERROR:   "error",
	}

	status, ok := statusMap[build.Raw.Result]
	if !ok {
		status = "error"
		s.logger.Warn().Str("jenkins_status", build.Raw.Result).Msg("unknown jenkins build status")
	}

	// Update the record with final status
	_, err = s.dbClient.DevBuild.UpdateOneID(recordID).
		SetStatus(status).
		SetUpdatedAt(time.Now()).
		Save(ctx)

	if err != nil {
		s.logger.Err(err).
			Int("record_id", recordID).
			Str("status", status).
			Msg("failed to update build record status")
		return
	}

	s.logger.Info().
		Str("job", s.jenkins.jobName).
		Int64("build_number", build.GetBuildNumber()).
		Str("result", status).
		Msg("build completed")
}
