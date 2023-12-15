package dl

import (
	"context"
	"log"

	health "github.com/PingCAP-QE/ee-apps/dl/gen/health"
)

// health service example implementation.
// The example methods log the requests and return zero values.
type healthsrvc struct {
	logger *log.Logger
}

// NewHealth returns the health service implementation.
func NewHealth(logger *log.Logger) health.Service {
	return &healthsrvc{logger}
}

// Healthz implements healthz.
func (s *healthsrvc) Healthz(ctx context.Context) (res bool, err error) {
	s.logger.Print("health.healthz")
	return true, nil
}

// Livez implements livez.
func (s *healthsrvc) Livez(ctx context.Context) (res bool, err error) {
	s.logger.Print("health.livez")
	return true, nil
}
