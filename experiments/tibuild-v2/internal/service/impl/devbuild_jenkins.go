package impl

import (
	"context"
	"fmt"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

func (s *devbuildsrvc) triggerJenkinsBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	const jobName = "hello"
	// TODO: trigger the actual build process according to the record.
	//
	// 1. Call the jenkins api to trigger the build.
	queueID, err := s.jenkinsClient.BuildJob(ctx, jobName, nil)
	if err != nil {
		s.logger.Err(err).Msg("failed to trigger jenkins build")
		return nil, err
	}

	// 2. Wait for the build to finish, be async mode.
	build, err := s.jenkinsClient.GetBuildFromQueueID(ctx, queueID)
	if err != nil {
		s.logger.Err(err).Msg("failed to get build from queue")
		return nil, err
	}

	// Wait for build to finish
	for build.IsRunning(ctx) {
		time.Sleep(5000 * time.Millisecond)
	}

	fmt.Printf("build number %d with result: %v\n", build.GetBuildNumber(), build.GetResult())

	// 3. Update the record with the build status, be async mode.
	//

	return nil, nil
}
