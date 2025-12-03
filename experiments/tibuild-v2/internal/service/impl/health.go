package impl

import (
	"context"

	"github.com/rs/zerolog"

	health "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/health"
)

// health service implementation.
// The health service provides health check endpoints.
type healthsrvc struct {
	logger *zerolog.Logger
}

// NewHealth returns the health service implementation.
func NewHealth(logger *zerolog.Logger) health.Service {
	return &healthsrvc{
		logger: logger,
	}
}

// Healthz implements healthz.
func (s *healthsrvc) Healthz(ctx context.Context) (res bool, err error) {
	s.logger.Debug().Msg("health.healthz")
	return true, nil
}

// Livez implements livez.
func (s *healthsrvc) Livez(ctx context.Context) (res bool, err error) {
	s.logger.Debug().Msg("health.livez")
	return true, nil
}
