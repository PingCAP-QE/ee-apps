package impl

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/bndr/gojenkins"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// devbuild service example implementation.
// The example methods log the requests and return zero values.
type devbuildsrvc struct {
	logger                 *zerolog.Logger
	dbClient               *ent.Client
	productRepoMap         map[string]string
	imageMirrorURLMap      map[string]string
	ghClient               *github.Client
	tektonCloudEventClient cloudevents.Client
	jenkins                struct {
		client  *gojenkins.Jenkins
		jobName string
	}
}

func NewDevbuild(logger *zerolog.Logger, cfg *config.Service) devbuild.Service {
	dbClient, err := newStoreClient(cfg.Store)
	if err != nil {
		logger.Err(err).Msg("failed to create store client")
		return nil
	}
	dbClient = dbClient.Debug()

	client, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(cfg.Tekton.CloudeventEndpoint))
	if err != nil {
		logger.Err(err).Msg("failed to create cloud event client")
		return nil
	}

	srvc := devbuildsrvc{
		logger:                 logger,
		dbClient:               dbClient.Debug(),
		productRepoMap:         cfg.ProductRepoMap,
		imageMirrorURLMap:      cfg.ImageMirrorURLMap,
		ghClient:               github.NewClientWithEnvProxy().WithAuthToken(cfg.Github.Token),
		tektonCloudEventClient: client,
	}
	srvc.jenkins.client = gojenkins.CreateJenkins(http.DefaultClient, cfg.Jenkins.URL)
	srvc.jenkins.jobName = cfg.Jenkins.JobName
	return &srvc
}

// List devbuild with pagination support
func (s *devbuildsrvc) List(ctx context.Context, p *devbuild.ListPayload) ([]*devbuild.DevBuild, error) {
	s.logger.Info().Msgf("devbuild.list")
	query := s.dbClient.DevBuild.Query().
		Where(entdevbuild.IsHotfix(p.Hotfix)).
		Offset(p.PageSize * (p.Page - 1)).
		Limit(p.PageSize)
	if p.CreatedBy != nil {
		query.Where(entdevbuild.CreatedBy(*p.CreatedBy))
	}
	if p.Sort != "" {
		if p.Direction == "desc" {
			query.Order(ent.Desc(p.Sort))
		} else {
			query.Order(ent.Asc(p.Sort))
		}
	}

	builds, err := query.All(ctx)
	if err != nil {
		if ent.IsNotFound(err) {
			s.logger.Debug().Msg("no builds found.")
			return nil, nil
		}
		s.logger.Err(err).Msg("internal error happened!")
		return nil, &devbuild.HTTPError{Code: http.StatusInternalServerError, Message: err.Error()}
	}

	var res []*devbuild.DevBuild
	for _, build := range builds {
		res = append(res, transformDevBuild(build))
	}

	return res, nil
}

// Create and trigger devbuild
func (s *devbuildsrvc) Create(ctx context.Context, p *devbuild.CreatePayload) (*devbuild.DevBuild, error) {
	s.logger.Info().Msgf("devbuild.create")

	// 1. insert a new record into the database
	record, err := s.newBuildEntity(ctx, p)
	if err != nil {
		return nil, err
	}
	s.logger.Debug().Any("record", record).Msg("record saved")
	// 1.1 fast return when it is a dry run.
	if p.Dryrun {
		return transformDevBuild(record), nil
	}

	// 2. trigger the actual build process according to the record.
	record, err = s.triggerBuild(ctx, record)
	if err != nil {
		return nil, err
	}

	s.logger.Debug().Any("record", record).Msg("record saved")

	// 3. fast feedback without waiting for the build to complete.
	return transformDevBuild(record), nil
}

// Get devbuild
func (s *devbuildsrvc) Get(ctx context.Context, p *devbuild.GetPayload) (*devbuild.DevBuild, error) {
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

	res := transformDevBuild(build)
	if res.Status.BuildReport == nil {
		return res, nil
	}

	// Append the internal image URL to the image list
	for i, img := range res.Status.BuildReport.Images {
		if img.InternalURL == nil {
			res.Status.BuildReport.Images[i].InternalURL = s.getInternalImageURL(img.URL)
		}
	}

	return res, nil
}

// Update devbuild status
func (s *devbuildsrvc) Update(ctx context.Context, p *devbuild.UpdatePayload) (res *devbuild.DevBuild, err error) {
	s.logger.Info().Msgf("devbuild.update")

	updater := s.dbClient.DevBuild.UpdateOneID(p.ID)
	if p.Status != nil {
		updater.SetStatus(string(p.Status.Status))
	}
	if p.Status.TektonStatus != nil {
		updater.SetTektonStatus(map[string]any{"pipelines": p.Status.TektonStatus.Pipelines})
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

func (s *devbuildsrvc) IngestEvent(ctx context.Context, p *devbuild.CloudEventIngestEventPayload) (res *devbuild.CloudEventResponse, err error) {
	s.logger.Info().Msgf("devbuild.ingestEvent")
	return nil, nil
}

func (s *devbuildsrvc) getInternalImageURL(img string) *string {
	for srcPrefix, dstPrefix := range s.imageMirrorURLMap {
		if strings.HasPrefix(img, srcPrefix) {
			ret := strings.Replace(img, srcPrefix, dstPrefix, 1)
			return &ret
		}
	}

	return nil
}

func newStoreClient(cfg config.Store) (*ent.Client, error) {
	db, err := ent.Open(cfg.Driver, cfg.DSN)
	if err != nil {
		return nil, err
	}

	// Run the auto migration tool.
	if err := db.Schema.Create(context.Background()); err != nil {
		return nil, err
	}

	return db, nil
}
