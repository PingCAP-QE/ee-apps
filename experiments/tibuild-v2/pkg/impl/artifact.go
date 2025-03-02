package impl

import (
	"context"

	artifact "github.com/PingCAP-QE/ee-apps/tibuild/gen/artifact"
	"github.com/rs/zerolog"
)

// artifact service example implementation.
// The example methods log the requests and return zero values.
type artifactsrvc struct {
	logger *zerolog.Logger
}

// NewArtifact returns the artifact service implementation.
func NewArtifact(logger *zerolog.Logger) artifact.Service {
	return &artifactsrvc{
		logger: logger,
	}
}

// Sync hotfix image to dockerhub
func (s *artifactsrvc) SyncImage(ctx context.Context, p *artifact.SyncImagePayload) (res *artifact.ImageSyncRequest, err error) {
	res = &artifact.ImageSyncRequest{}
	s.logger.Info().Msgf("artifact.syncImage")
	return
}
