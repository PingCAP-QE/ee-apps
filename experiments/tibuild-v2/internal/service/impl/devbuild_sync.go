package impl

import (
	"context"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// implement the method: syncBuildStatus
func (s *devbuildsrvc) syncBuildStatus(ctx context.Context, build *ent.DevBuild) (*ent.DevBuild, error) {
	// TODO: Implement the logic to sync the build status
	return nil, nil
}
