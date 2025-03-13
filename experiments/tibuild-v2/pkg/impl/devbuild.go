package impl

import (
	"context"

	devbuild "github.com/PingCAP-QE/ee-apps/tibuild/gen/devbuild"
	"github.com/rs/zerolog"
)

// devbuild service example implementation.
// The example methods log the requests and return zero values.
type devbuildsrvc struct {
	logger *zerolog.Logger
}

// NewDevbuild returns the devbuild service implementation.
func NewDevbuild(logger *zerolog.Logger) devbuild.Service {
	return &devbuildsrvc{
		logger: logger,
	}
}

// List devbuild with pagination support
func (s *devbuildsrvc) List(ctx context.Context, p *devbuild.ListPayload) (res []*devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.list")
	return
}

// Create and trigger devbuild
func (s *devbuildsrvc) Create(ctx context.Context, p *devbuild.CreatePayload) (res *devbuild.DevBuild, err error) {
	res = &devbuild.DevBuild{}
	s.logger.Info().Msgf("devbuild.create")
	return
}

// Get devbuild
func (s *devbuildsrvc) Get(ctx context.Context, p *devbuild.GetPayload) (res *devbuild.DevBuild, err error) {
	res = &devbuild.DevBuild{}
	s.logger.Info().Msgf("devbuild.get")
	return
}

// Update devbuild status
func (s *devbuildsrvc) Update(ctx context.Context, p *devbuild.UpdatePayload) (res *devbuild.DevBuild, err error) {
	res = &devbuild.DevBuild{}
	s.logger.Info().Msgf("devbuild.update")
	return
}

// Rerun devbuild
func (s *devbuildsrvc) Rerun(ctx context.Context, p *devbuild.RerunPayload) (res *devbuild.DevBuild, err error) {
	res = &devbuild.DevBuild{}
	s.logger.Info().Msgf("devbuild.rerun")
	return
}
