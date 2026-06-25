package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
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
	dashboardURL           string
	httpClient             *http.Client
	jenkins                struct {
		client  *gojenkins.Jenkins
		jobName string
	}
}

func NewDevbuild(logger *zerolog.Logger, cfg *config.Service) devbuild.Service {
	dbClient, err := NewStoreClient(cfg.Store)
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
		dashboardURL:           cfg.Tekton.ViewURL,
		httpClient:             &http.Client{Timeout: 30 * time.Second},
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
		SetPlatform(existingBuild.Platform).
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

	// Extract DevBuild ID from PipelineRun annotations
	buildID, err := s.extractDevBuildID(p.Data, p.Source)
	if err != nil {
		s.logger.Err(err).Msg("failed to extract devbuild id from event")
		return nil, &devbuild.HTTPError{Code: http.StatusBadRequest, Message: err.Error()}
	}
	if buildID == 0 {
		return nil, &devbuild.HTTPError{Code: http.StatusBadRequest, Message: "not a devbuild event"}
	}

	// Get the existing build
	build, err := s.dbClient.DevBuild.Get(ctx, buildID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, &devbuild.HTTPError{Code: http.StatusNotFound, Message: "Devbuild not found"}
		}
		return nil, err
	}

	// Extract status from event type
	status := s.extractBuildStatusFromEventType(p.Type)

	// Update build status
	updater := s.dbClient.DevBuild.UpdateOneID(build.ID)
	if status != "" {
		updater.SetStatus(status)
	}
	updater.SetUpdatedAt(time.Now())

	// Update tekton status if available
	if tektonStatus := s.extractTektonStatus(p.Data); tektonStatus != nil {
		updater.SetTektonStatus(tektonStatus)
	}

	if _, err := updater.Save(ctx); err != nil {
		s.logger.Err(err).Msg("failed to update build status from event")
		return nil, err
	}

	return &devbuild.CloudEventResponse{
		ID:      p.ID,
		Status:  "accepted",
		Message: ptr("Event processed successfully"),
	}, nil
}

func (s *devbuildsrvc) extractDevBuildID(data any, source string) (int, error) {
	// First try to extract from PipelineRun annotations (Tekton callback)
	if data != nil {
		if dataMap, ok := data.(map[string]any); ok {
			if pipelineRun, ok := dataMap["pipelineRun"].(map[string]any); ok {
				if metadata, ok := pipelineRun["metadata"].(map[string]any); ok {
					if annotations, ok := metadata["annotations"].(map[string]any); ok {
						if ceContext, ok := annotations["tekton.dev/ce-context"].(string); ok {
							var context struct {
								Source  string `json:"source"`
								Subject string `json:"subject"`
							}
							if err := json.Unmarshal([]byte(ceContext), &context); err == nil {
								if strings.Contains(context.Source, "tibuild.pingcap.net/api/devbuild") {
									return strconv.Atoi(context.Subject)
								}
							}
						}
					}
				}
			}
		}
	}

	// Fallback: try to extract from event source
	if strings.Contains(source, "tibuild.pingcap.net/api/devbuilds/") {
		parts := strings.Split(source, "/")
		if len(parts) > 0 {
			return strconv.Atoi(parts[len(parts)-1])
		}
	}

	return 0, nil
}

func (s *devbuildsrvc) extractBuildStatusFromEventType(eventType string) string {
	switch eventType {
	case "dev.tekton.event.pipelinerun.started.v1":
		return "running"
	case "dev.tekton.event.pipelinerun.successful.v1":
		return "success"
	case "dev.tekton.event.pipelinerun.failed.v1":
		return "failure"
	default:
		return ""
	}
}

func (s *devbuildsrvc) extractTektonStatus(data any) map[string]any {
	if data == nil {
		return nil
	}

	dataMap, ok := data.(map[string]any)
	if !ok {
		return nil
	}

	// Extract pipeline run info from event data
	pipelineName, ok := dataMap["pipelineName"].(string)
	if !ok || pipelineName == "" {
		return nil
	}

	pipeline := map[string]any{
		"name": pipelineName,
	}
	if status, ok := dataMap["status"].(string); ok {
		pipeline["status"] = status
	}
	if startTime, ok := dataMap["startTime"].(string); ok {
		pipeline["start_at"] = startTime
	}
	if endTime, ok := dataMap["endTime"].(string); ok {
		pipeline["end_at"] = endTime
	}

	return map[string]any{
		"pipelines": []map[string]any{pipeline},
	}
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

// NewStoreClient creates a new Ent client and runs auto migration.
func NewStoreClient(cfg config.Store) (*ent.Client, error) {
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
