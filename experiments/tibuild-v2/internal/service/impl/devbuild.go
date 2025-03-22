package impl

import (
	"context"
	"net/http"
	"time"

	"entgo.io/ent/dialect/sql"
	"github.com/bndr/gojenkins"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// devbuild service example implementation.
// The example methods log the requests and return zero values.
type devbuildsrvc struct {
	logger         *zerolog.Logger
	dbClient       *ent.Client
	productRepoMap map[string]string
	ghClient       *github.Client
	tektonClient   tektonClient
	jenkinsClient  *gojenkins.Jenkins
	tknListenerURL string
}

type tektonClient struct {
	client      cloudevents.Client
	listenerURL string
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
		if ent.IsNotFound(err) {
			return nil, nil
		}
		return nil, &devbuild.HTTPError{Code: http.StatusInternalServerError, Message: err.Error()}
	}

	for _, build := range builds {
		res = append(res, transformDevBuild(build))
	}

	return res, nil
}

// Create and trigger devbuild
func (s *devbuildsrvc) Create(ctx context.Context, p *devbuild.CreatePayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.create")

	// 1. insert a new record into the database
	record, err := s.newBuildEntity(ctx, p)
	if err != nil {
		return nil, err
	}
	// 1.1 fast return when it is a dry run.
	if p.Dryrun {
		return transformDevBuild(record), nil
	}

	// 2. trigger the actual build process according to the record.
	record, err = s.triggerTknBuild(ctx, record)
	if err != nil {
		return nil, err
	}

	// 3. fast feedback without waiting for the build to complete.
	return transformDevBuild(record), nil
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

	// When user requests sync the build status.
	if p.Sync {
		if _, err := s.syncBuildStatus(ctx, build); err != nil {
			return nil, err
		}
	}

	return transformDevBuild(build), nil
}

// Update devbuild status
func (s *devbuildsrvc) Update(ctx context.Context, p *devbuild.UpdatePayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.update")

	updater := s.dbClient.DevBuild.UpdateOneID(p.ID)
	if p.Build.Status != nil {
		updater.SetStatus(string(p.Build.Status.Status))
	}
	if p.Build.Status.TektonStatus != nil {
		updater.SetTektonStatus(map[string]any{"pipelines": p.Build.Status.TektonStatus.Pipelines})
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
		SetGitSha(existingBuild.GitSha).
		SetPluginGitRef(existingBuild.PluginGitRef).
		SetIsHotfix(existingBuild.IsHotfix).
		SetIsPushGcr(existingBuild.IsPushGcr).
		SetStatus("pending").
		SetCreatedAt(time.Now()).
		Save(ctx)
	if err != nil {
		return nil, err
	}

	if _, err := s.triggerTknBuild(ctx, newBuild); err != nil {
		return nil, err
	}

	return transformDevBuild(newBuild), nil
}
