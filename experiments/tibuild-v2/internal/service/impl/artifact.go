package impl

import (
	"context"
	"fmt"
	"net/http"

	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/rs/zerolog"

	artifact "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
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

// SyncImage copies a Docker image from the source registry to the target registry.
//
// When running in k8s pod, it should use the service account that has Docker authentication
// configured and appended to its context.
//
// When debugging locally, it will use the default authentication stored in the
// Docker config.json file (~/.docker/config.json).
func (s *artifactsrvc) SyncImage(ctx context.Context, p *artifact.ImageSyncRequest) (res *artifact.ImageSyncRequest, err error) {
	// skip validate input parameters, since the http access layer will do the validation.

	l := s.logger.With().
		Str("source", p.Source).
		Str("target", p.Target).
		Logger()

	l.Info().Msg("Syncing Docker image")

	// Create options for crane operations
	options := []crane.Option{crane.WithContext(ctx)}

	// Use the crane library to copy the image directly between registries
	if err := crane.Copy(p.Source, p.Target, options...); err != nil {
		l.Err(err).Msg("Failed to sync image")

		return nil, &artifact.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to sync image: %v", err),
		}
	}

	l.Info().Msg("Image successfully synced to DockerHub")

	res = p
	return res, nil
}
