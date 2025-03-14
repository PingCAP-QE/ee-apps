package impl

import (
	"context"
	"net/http"
	"time"

	"entgo.io/ent/dialect/sql"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// devbuild service example implementation.
// The example methods log the requests and return zero values.
type devbuildsrvc struct {
	logger   *zerolog.Logger
	dbClient *ent.Client
}

func NewDevbuild(logger *zerolog.Logger, client *ent.Client) devbuild.Service {
	return &devbuildsrvc{
		logger:   logger,
		dbClient: client,
	}
}

// List devbuild with pagination support
func (s *devbuildsrvc) List(ctx context.Context, p *devbuild.ListPayload) (res []*devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.list")
	query := s.dbClient.DevBuild.Query().
		Where(entdevbuild.IsHotfix(p.Hotfix)).
		Offset(p.PageSize * (p.Page - 1)).
		Limit(p.PageSize).
		Order(sql.OrderByField(p.Sort).ToFunc())

	if p.CreatedBy != nil {
		query.Where(entdevbuild.CreatedBy(*p.CreatedBy))
	}

	builds, err := query.All(ctx)
	if err != nil {
		return nil, err
	}

	for _, build := range builds {
		res = append(res, transformDevBuild(build))
	}

	return res, nil
}

// Create and trigger devbuild
func (s *devbuildsrvc) Create(ctx context.Context, p *devbuild.CreatePayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.create")

	create := s.dbClient.DevBuild.Create().
		SetCreatedBy(p.CreatedBy).
		SetGitRef(p.Request.GitRef).
		SetEdition(string(p.Request.Edition)).
		SetNillableIsHotfix(p.Request.IsHotfix).
		SetCreatedAt(time.Now()).
		SetCreatedBy(p.CreatedBy).
		SetStatus("pending")

	// TODO: get the commit sha and set it in `create`.

	build, err := create.Save(ctx)
	if err != nil {
		return nil, err
	}

	// TODO: trigger the actual build process
	// This could involve calling a CI system or other build service

	return transformDevBuild(build), nil
}

// Get devbuild
func (s *devbuildsrvc) Get(ctx context.Context, p *devbuild.GetPayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.get")

	build, err := s.dbClient.DevBuild.Get(ctx, p.ID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, &devbuild.HTTPError{Code: http.StatusNotFound, Message: "Devbuild not found"}
		}
		return nil, err
	}

	return transformDevBuild(build), nil
}

// Update devbuild status
func (s *devbuildsrvc) Update(ctx context.Context, p *devbuild.UpdatePayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.update")

	updater := s.dbClient.DevBuild.UpdateOneID(p.ID)
	if p.DevBuild.Status != nil {
		updater.SetStatus(string(p.DevBuild.Status.Status))
	}
	updater.SetUpdatedAt(time.Now())

	build, err := updater.Save(ctx)

	if err != nil {
		if ent.IsNotFound(err) {
			return nil, &devbuild.HTTPError{Code: http.StatusNotFound, Message: "Devbuild not found"}
		}
		return nil, err
	}

	return transformDevBuild(build), nil
}

// Rerun devbuild
func (s *devbuildsrvc) Rerun(ctx context.Context, p *devbuild.RerunPayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.rerun")

	// First get the existing build
	existingBuild, err := s.dbClient.DevBuild.Get(ctx, p.ID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, &devbuild.HTTPError{Code: http.StatusNotFound, Message: "Devbuild not found"}
		}
		return nil, err
	}

	// Create a new build with the same parameters
	newBuild, err := s.dbClient.DevBuild.Create().
		SetCreatedBy(existingBuild.CreatedBy).
		SetProduct(existingBuild.Product).
		SetEdition(existingBuild.Edition).
		SetVersion(existingBuild.Version).
		SetGithubRepo(existingBuild.GithubRepo).
		SetGitRef(existingBuild.GitRef).
		SetGitHash(existingBuild.GitHash).
		SetPluginGitRef(existingBuild.PluginGitRef).
		SetIsHotfix(existingBuild.IsHotfix).
		SetIsPushGcr(existingBuild.IsPushGcr).
		SetStatus("pending").
		SetCreatedAt(time.Now()).
		Save(ctx)
	if err != nil {
		return nil, err
	}

	// TODO: trigger the actual build process again

	return transformDevBuild(newBuild), nil
}
