package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/bndr/gojenkins"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/predicate"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
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
	ociFileDownloadURL     string
	reconcilerSince        time.Duration
	httpClient             *http.Client
	larkNotifier           *LarkNotifier
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

	client, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(cfg.Tekton.CloudeventEndpoint))
	if err != nil {
		logger.Err(err).Msg("failed to create cloud event client")
		return nil
	}

	reconcilerSince := 24 * time.Hour
	if cfg.Tekton.ReconcilerSince != "" {
		if d, err := time.ParseDuration(cfg.Tekton.ReconcilerSince); err == nil {
			reconcilerSince = d
		} else {
			logger.Warn().Str("reconciler_since", cfg.Tekton.ReconcilerSince).Msg("invalid reconciler_since, using default 24h")
		}
	}

	srvc := devbuildsrvc{
		logger:                 logger,
		dbClient:               dbClient,
		productRepoMap:         cfg.ProductRepoMap,
		imageMirrorURLMap:      cfg.ImageMirrorURLMap,
		ghClient:               github.NewClientWithEnvProxy().WithAuthToken(cfg.Github.Token),
		tektonCloudEventClient: client,
		dashboardURL:           cfg.Tekton.ViewURL,
		ociFileDownloadURL:     strings.TrimRight(cfg.Tekton.OciFileDownloadURL, "/"),
		reconcilerSince:        reconcilerSince,
		httpClient:             &http.Client{Timeout: 30 * time.Second},
	}
	srvc.jenkins.client = gojenkins.CreateJenkins(http.DefaultClient, cfg.Jenkins.URL)
	srvc.jenkins.jobName = cfg.Jenkins.JobName

	// Register Lark notification hook if enabled
	if cfg.Lark.Enabled && cfg.Lark.AppID != "" && cfg.Lark.AppSecret != "" {
		srvc.larkNotifier = NewLarkNotifier(cfg.Lark.AppID, cfg.Lark.AppSecret, cfg.Lark.Channels, logger)
		registerNotificationHook(dbClient, srvc.larkNotifier, logger)
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
	if p.Scope == "mine" {
		user, ok := identity.FromContext(ctx)
		if !ok {
			return nil, &devbuild.DevBuildUnauthorizedError{Code: http.StatusUnauthorized, Message: "authenticated identity required for mine scope"}
		}
		query.Where(entdevbuild.CreatedByEqualFold(user.Email))
	}
	if p.Status != nil {
		query.Where(entdevbuild.Status(string(*p.Status)))
	}
	if p.Product != nil && *p.Product != "" {
		query.Where(entdevbuild.Product(*p.Product))
	}
	if p.Q != nil && strings.TrimSpace(*p.Q) != "" {
		term := strings.TrimSpace(*p.Q)
		predicates := []predicate.DevBuild{
			entdevbuild.GitRefContainsFold(term),
			entdevbuild.CreatedByContainsFold(term),
		}
		if id, err := strconv.Atoi(term); err == nil {
			predicates = append(predicates, entdevbuild.ID(id))
		}
		query.Where(entdevbuild.Or(predicates...))
	}
	// Map camelCase sort values to Ent column names
	sortColumnMap := map[string]string{
		"createdAt": "created_at",
		"updatedAt": "updated_at",
	}
	if col, ok := sortColumnMap[p.Sort]; ok {
		if p.Direction == "desc" {
			query.Order(ent.Desc(col))
		} else {
			query.Order(ent.Asc(col))
		}
	}

	builds, err := query.All(ctx)
	if err != nil {
		if ent.IsNotFound(err) {
			s.logger.Debug().Msg("no builds found.")
			return nil, nil
		}
		s.logger.Err(err).Msg("internal error happened!")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to list builds"}
	}

	var res []*devbuild.DevBuild
	for _, build := range builds {
		item := transformDevBuild(build)
		s.applyPermissions(ctx, item)
		res = append(res, item)
	}

	return res, nil
}

// Capabilities lists the products currently configured by the service. Portal
// defaults are deliberately conservative and do not change legacy API support.
func (s *devbuildsrvc) Capabilities(context.Context) (*devbuild.DevBuildCapabilities, error) {
	products := make([]string, 0, len(s.productRepoMap))
	for product := range s.productRepoMap {
		products = append(products, product)
	}
	sort.Strings(products)
	result := &devbuild.DevBuildCapabilities{
		PipelineEngines:       []string{tektonEngine},
		DefaultPipelineEngine: tektonEngine,
	}
	for _, product := range products {
		result.Products = append(result.Products, &devbuild.DevBuildProductCapability{
			ID:              product,
			Label:           productLabel(product),
			Editions:        []string{"community"},
			Platforms:       []string{"linux", "linux/amd64", "linux/arm64"},
			DefaultEdition:  "community",
			DefaultPlatform: "linux",
		})
	}
	return result, nil
}

// Create and trigger devbuild
func (s *devbuildsrvc) Create(ctx context.Context, p *devbuild.CreatePayload) (*devbuild.DevBuild, error) {
	s.logger.Info().Msgf("devbuild.create")
	if user, ok := identity.FromContext(ctx); ok {
		p.CreatedBy = &user.Email
	} else if p.CreatedBy == nil || strings.TrimSpace(*p.CreatedBy) == "" {
		return nil, &devbuild.DevBuildUnauthorizedError{Code: http.StatusUnauthorized, Message: "createdBy or authenticated identity is required"}
	}

	// 1. insert a new record into the database
	record, err := s.newBuildEntity(ctx, p)
	if err != nil {
		if _, ok := err.(*devbuild.DevBuildBadRequestError); ok {
			return nil, err
		}
		s.logger.Err(err).Msg("failed to create build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to create build"}
	}
	s.logger.Debug().Any("record", record).Msg("record saved")
	// 1.1 fast return when it is a dry run.
	if p.Dryrun {
		return transformDevBuild(record), nil
	}

	// 2. trigger the actual build process according to the record.
	recordID := record.ID
	record, err = s.triggerBuild(ctx, record)
	if err != nil {
		s.logger.Err(err).Int("build_id", recordID).Msg("failed to trigger build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to trigger build"}
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
			return nil, &devbuild.DevBuildNotFoundError{Code: http.StatusNotFound, Message: "build not found"}
		}
		s.logger.Err(err).Int("build_id", p.ID).Msg("failed to get build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to get build"}
	}

	res := transformDevBuild(build)
	s.applyPermissions(ctx, res)
	if res.Status.BuildReport == nil {
		return res, nil
	}
	s.addArtifactURLs(res)

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
			TriggersEventIds: p.Status.TektonStatus.TriggersEventIDs,
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
			return nil, &devbuild.DevBuildNotFoundError{Code: http.StatusNotFound, Message: "build not found"}
		}
		s.logger.Err(err).Int("build_id", p.ID).Msg("failed to update build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to update build"}
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
			return nil, &devbuild.DevBuildNotFoundError{Code: http.StatusNotFound, Message: "build not found"}
		}
		s.logger.Err(err).Int("build_id", p.ID).Msg("failed to get build for rerun")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to rerun build"}
	}
	if user, ok := identity.FromContext(ctx); ok && !strings.EqualFold(user.Email, existingBuild.CreatedBy) {
		return nil, &devbuild.DevBuildForbiddenError{Code: http.StatusForbidden, Message: "only the build creator can rerun this build"}
	}
	if !isTerminalStatus(existingBuild.Status) {
		return nil, &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "only terminal builds can be rerun"}
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
		SetIsPushGCR(existingBuild.IsPushGCR).
		SetPlatform(existingBuild.Platform).
		SetStatus("PENDING").
		SetCreatedAt(time.Now()).
		Save(ctx)
	if err != nil {
		s.logger.Err(err).Int("build_id", p.ID).Msg("failed to create rerun build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to rerun build"}
	}

	if _, err := s.triggerTknBuild(ctx, newBuild); err != nil {
		s.logger.Err(err).Int("build_id", newBuild.ID).Msg("failed to trigger rerun build")
		return nil, &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: "unable to trigger rerun build"}
	}

	res = transformDevBuild(newBuild)
	s.applyPermissions(ctx, res)
	return res, nil
}

func (s *devbuildsrvc) applyPermissions(ctx context.Context, build *devbuild.DevBuild) {
	canRerun := false
	if user, ok := identity.FromContext(ctx); ok {
		canRerun = strings.EqualFold(user.Email, build.Meta.CreatedBy) && isTerminalStatus(string(build.Status.Status))
	}
	build.Permissions = &devbuild.DevBuildPermissions{CanRerun: canRerun}
}

func productLabel(product string) string {
	labels := map[string]string{
		"tidb": "TiDB", "tikv": "TiKV", "tiflash": "TiFlash", "pd": "PD", "ticdc": "TiCDC",
		"dm": "DM", "br": "BR", "tiproxy": "TiProxy", "tidb-operator": "TiDB Operator",
	}
	if label := labels[product]; label != "" {
		return label
	}
	return product
}

func (s *devbuildsrvc) addArtifactURLs(build *devbuild.DevBuild) {
	if s.ociFileDownloadURL == "" || build.Status.BuildReport == nil {
		return
	}
	for _, artifact := range build.Status.BuildReport.Binaries {
		if artifact.OciFile != nil {
			downloadURL := buildArtifactURL(s.ociFileDownloadURL, artifact.OciFile)
			artifact.URL = &downloadURL
		}
		if artifact.Sha256OCIFile != nil {
			shaURL := buildArtifactURL(s.ociFileDownloadURL, artifact.Sha256OCIFile)
			artifact.Sha256URL = &shaURL
		}
	}
}

func buildArtifactURL(base string, file *devbuild.OciFile) string {
	u, err := url.Parse(base + "/" + strings.TrimLeft(file.Repo, "/"))
	if err != nil {
		return ""
	}
	query := u.Query()
	query.Set("tag", file.Tag)
	query.Set("file", file.File)
	u.RawQuery = query.Encode()
	return u.String()
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
// on every DevBuild status change. The first notification creates a new card;
// subsequent updates refresh the same card in place.
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

			// Trigger on Create (initial PENDING) and Update (status changes)
			if !m.Op().Is(ent.OpCreate | ent.OpUpdate | ent.OpUpdateOne) {
				return v, nil
			}

			// For updates, skip unless Status or TektonStatus was changed.
			// This catches: overall status change, and individual pipeline run progress.
			if m.Op().Is(ent.OpUpdate | ent.OpUpdateOne) {
				_, hasStatus := mut.Status()
				_, hasTektonStatus := mut.TektonStatus()
				if !hasStatus && !hasTektonStatus {
					return v, nil
				}
			}

			// Get the build ID and send notification asynchronously
			buildID, _ := mut.ID()
			go func() {
				build, err := dbClient.DevBuild.Get(context.Background(), buildID)
				if err != nil {
					logger.Err(err).Int("build_id", buildID).Msg("failed to get build for notification")
					return
				}

				newState, notifyErr := notifier.Notify(context.Background(), build)
				if notifyErr != nil {
					logger.Err(notifyErr).Int("build_id", buildID).Msg("failed to send notification")
					return
				}

				// Persist the updated notification state (message IDs for each channel).
				if newState != nil {
					if _, err := dbClient.DevBuild.UpdateOneID(build.ID).
						SetNotificationState(*newState).
						Save(context.Background()); err != nil {
						logger.Err(err).Int("build_id", buildID).Msg("failed to store notification state")
					}
				}
			}()

			return v, nil
		})
	})
}
