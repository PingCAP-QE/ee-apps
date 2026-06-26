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
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
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
		dashboardURL:           cfg.Tekton.ViewURL,
		httpClient:             &http.Client{Timeout: 30 * time.Second},
	}
	srvc.jenkins.client = gojenkins.CreateJenkins(http.DefaultClient, cfg.Jenkins.URL)
	srvc.jenkins.jobName = cfg.Jenkins.JobName

	// Register Lark notification hook if enabled
	if cfg.Lark.Enabled && cfg.Lark.WebhookURL != "" {
		notifier := NewLarkNotifier(cfg.Lark.WebhookURL, logger)
		registerNotificationHook(dbClient, notifier, logger)
		logger.Info().Msg("lark notification hook registered")
	}

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
		// Convert Goa TektonStatus to schema TektonStatus
		tektonStatus := schema.TektonStatus{
			TriggersEventIds: p.Status.TektonStatus.TriggersEventIds,
		}
		// Convert pipelines if present
		if len(p.Status.TektonStatus.Pipelines) > 0 {
			pipelines := make([]schema.TektonPipeline, 0, len(p.Status.TektonStatus.Pipelines))
			for _, p := range p.Status.TektonStatus.Pipelines {
				pipeline := schema.TektonPipeline{
					Name:     p.Name,
					Status:   string(p.Status),
					Platform: derefString(p.Platform),
					URL:      derefString(p.URL),
					GitSha:   derefString(p.GitSha),
				}
				if p.StartAt != nil {
					t, err := time.Parse(time.RFC3339, *p.StartAt)
					if err == nil {
						pipeline.StartAt = &t
					}
				}
				if p.EndAt != nil {
					t, err := time.Parse(time.RFC3339, *p.EndAt)
					if err == nil {
						pipeline.EndAt = &t
					}
				}
				pipelines = append(pipelines, pipeline)
			}
			tektonStatus.Pipelines = pipelines
		}
		updater.SetTektonStatus(tektonStatus)
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

func (s *devbuildsrvc) getInternalImageURL(img string) *string {
	for srcPrefix, dstPrefix := range s.imageMirrorURLMap {
		if strings.HasPrefix(img, srcPrefix) {
			ret := strings.Replace(img, srcPrefix, dstPrefix, 1)
			return &ret
		}
	}

	return nil
}

// derefString safely dereferences a string pointer, returning empty string if nil.
func derefString(s *string) string {
	if s == nil {
		return ""
	}
	return *s
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

// registerNotificationHook registers an Ent hook that sends Lark notifications
// when a DevBuild reaches a terminal status (success, failure, error, aborted).
func registerNotificationHook(dbClient *ent.Client, notifier Notifier, logger *zerolog.Logger) {
	dbClient.Use(func(next ent.Mutator) ent.Mutator {
		return ent.MutateFunc(func(ctx context.Context, m ent.Mutation) (ent.Value, error) {
			v, err := next.Mutate(ctx, m)
			if err != nil {
				return v, err
			}

			// Check if this is a DevBuild update with status change
			mut, ok := m.(*ent.DevBuildMutation)
			if !ok {
				return v, nil
			}

			// Only trigger on Update operations
			if !m.Op().Is(ent.OpUpdate) {
				return v, nil
			}

			// Check if status field was changed
			newStatus, exists := mut.Status()
			if !exists || !isTerminalStatus(newStatus) {
				return v, nil
			}

			// Get the build ID and fetch the latest record
			buildID, _ := mut.ID()
			build, err := dbClient.DevBuild.Get(ctx, buildID)
			if err != nil {
				logger.Err(err).Int("build_id", buildID).Msg("failed to get build for notification")
				return v, nil
			}

			// Send notification asynchronously (non-blocking)
			go func() {
				notifCtx := context.Background()
				if notifyErr := notifier.Notify(notifCtx, build); notifyErr != nil {
					logger.Err(notifyErr).Int("build_id", buildID).Msg("failed to send notification")
				}
			}()

			return v, nil
		})
	})
}
