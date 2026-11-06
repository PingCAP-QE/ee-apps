package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entimagesynctask "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/imagesynctask"
	artifact "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// Task statuses of an image sync task.
const (
	TaskStatusPending    = "PENDING"
	TaskStatusProcessing = "PROCESSING"
	TaskStatusSucceeded  = "SUCCEEDED"
	TaskStatusFailed     = "FAILED"
)

// Worker is the background execution surface of the artifact service. It is
// implemented by artifactsrvc and shared by cmd/tibuild (in-process) and
// cmd/tibuild-worker (standalone).
type Worker interface {
	Start(context.Context)
	Stop()
	MetricsHandler() http.Handler
	MetricsGatherer() prometheus.Gatherer
	// Reload applies a validated configuration revision to the running worker.
	Reload(*config.Service)
}

// NewWorker builds the standalone background executor. The image sync queue and
// the publish step executor share one process and one set of per-target
// registry credentials.
func NewWorker(logger *zerolog.Logger, cfg *config.Service) (Worker, error) {
	artifactSvc := NewArtifact(logger, cfg)
	if artifactSvc == nil {
		return nil, errors.New("failed to initialize artifact service")
	}
	artifactWorker, ok := artifactSvc.(Worker)
	if !ok {
		return nil, errors.New("artifact service does not implement Worker")
	}

	publishSvc := NewPublish(logger, cfg)
	if publishSvc == nil {
		return nil, errors.New("failed to initialize publish service")
	}
	publishWorker, ok := publishSvc.(Worker)
	if !ok {
		return nil, errors.New("publish service does not implement Worker")
	}

	return &compositeWorker{workers: []Worker{artifactWorker, publishWorker}}, nil
}

// compositeWorker runs several workers in one process and stops them together.
type compositeWorker struct {
	workers []Worker
}

// Start runs every worker until ctx is cancelled.
func (w *compositeWorker) Start(ctx context.Context) {
	var wg sync.WaitGroup
	for _, worker := range w.workers {
		wg.Add(1)
		go func(worker Worker) {
			defer wg.Done()
			worker.Start(ctx)
		}(worker)
	}
	wg.Wait()
}

// Stop waits for every worker to drain its in-flight work.
func (w *compositeWorker) Stop() {
	for _, worker := range w.workers {
		worker.Stop()
	}
}

// Reload applies a validated revision to every worker that keeps runtime state.
func (w *compositeWorker) Reload(cfg *config.Service) {
	for _, worker := range w.workers {
		if reloader, ok := worker.(interface{ Reload(*config.Service) }); ok {
			reloader.Reload(cfg)
		}
	}
}

// MetricsGatherer merges the per-worker registries. A worker that only exposes
// an opaque handler is skipped here and served by MetricsHandler instead.
func (w *compositeWorker) MetricsGatherer() prometheus.Gatherer {
	gatherers := make(prometheus.Gatherers, 0, len(w.workers))
	for _, worker := range w.workers {
		if gatherer, ok := worker.(interface {
			MetricsGatherer() prometheus.Gatherer
		}); ok {
			if g := gatherer.MetricsGatherer(); g != nil {
				gatherers = append(gatherers, g)
			}
		}
	}
	if len(gatherers) == 0 {
		return nil
	}
	return gatherers
}

// MetricsHandler merges every worker registry into one endpoint.
func (w *compositeWorker) MetricsHandler() http.Handler {
	if gatherer := w.MetricsGatherer(); gatherer != nil {
		return promhttp.HandlerFor(gatherer, promhttp.HandlerOpts{})
	}
	for _, worker := range w.workers {
		if handler := worker.MetricsHandler(); handler != nil {
			return handler
		}
	}
	return nil
}

// artifactsrvc is the artifact service implementation.
type artifactsrvc struct {
	logger *zerolog.Logger
	db     *ent.Client

	mu         sync.RWMutex
	sourceRegx *regexp.Regexp
	targetRegx *regexp.Regexp

	pollingRate time.Duration
	workerCfg   config.Worker
	nowFn       func() time.Time
	metrics     *artifactMetrics
	alerts      *alertTracker
	alerter     AlertNotifier
	inflight    sync.WaitGroup
}

// NewArtifact returns the artifact service implementation.
func NewArtifact(logger *zerolog.Logger, cfg *config.Service) artifact.Service {
	dbClient, err := newStoreClient(cfg.Store)
	if err != nil {
		logger.Err(err).Msg("failed to create store client")
		return nil
	}

	s := &artifactsrvc{
		logger: logger,
		db:     dbClient,
	}
	s.metrics = newArtifactMetrics()
	s.alerts = newAlertTracker(newAlertSettings(cfg.Worker))
	if cfg.Lark.Enabled && cfg.Lark.AppID != "" && cfg.Lark.AppSecret != "" {
		s.alerter = NewLarkNotifier(cfg.Lark.AppID, cfg.Lark.AppSecret, cfg.Lark.Channels, logger)
	}
	s.applyConfig(cfg)
	return s
}

// applyConfig applies the image sync configuration at startup and on reload.
func (s *artifactsrvc) applyConfig(cfg *config.Service) {
	// Validate new regexes before applying them, keep the previous values on error.
	var src, dst *regexp.Regexp
	if cfg.ImageSync.SourceRegx != "" {
		r, err := regexp.Compile(cfg.ImageSync.SourceRegx)
		if err != nil {
			s.logger.Warn().Str("source_regx", cfg.ImageSync.SourceRegx).Msg("invalid image_sync.source_regx, keeping previous value")
		} else {
			src = r
		}
	}
	if cfg.ImageSync.TargetRegx != "" {
		r, err := regexp.Compile(cfg.ImageSync.TargetRegx)
		if err != nil {
			s.logger.Warn().Str("target_regx", cfg.ImageSync.TargetRegx).Msg("invalid image_sync.target_regx, keeping previous value")
		} else {
			dst = r
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if cfg.ImageSync.SourceRegx == "" {
		s.sourceRegx = nil
	} else if src != nil {
		s.sourceRegx = src
	}
	if cfg.ImageSync.TargetRegx == "" {
		s.targetRegx = nil
	} else if dst != nil {
		s.targetRegx = dst
	}
	if cfg.ImageSync.PollingRate != "" {
		if d, err := time.ParseDuration(cfg.ImageSync.PollingRate); err == nil && d > 0 {
			s.pollingRate = d
		} else {
			s.logger.Warn().Str("polling_rate", cfg.ImageSync.PollingRate).Msg("invalid image_sync.polling_rate, keeping previous value")
		}
	}
	// worker.poll_interval takes precedence over image_sync.polling_rate.
	if cfg.Worker.PollInterval != "" {
		if d, err := time.ParseDuration(cfg.Worker.PollInterval); err == nil && d > 0 {
			s.pollingRate = d
		} else {
			s.logger.Warn().Str("poll_interval", cfg.Worker.PollInterval).Msg("invalid worker.poll_interval, keeping previous value")
		}
	}
	workerCfg := cfg.Worker
	workerCfg.ApplyDefaults()
	s.workerCfg = workerCfg
	if s.alerts != nil {
		// Refresh thresholds in place so a reload cannot drop the dedupe state.
		s.alerts.setSettings(newAlertSettings(workerCfg))
	}
	if s.pollingRate <= 0 {
		s.pollingRate = defaultPollingRate
	}
}

// Reload updates the service configuration at runtime. It is called by the
// config reloader when the config file changes on disk.
func (s *artifactsrvc) Reload(cfg *config.Service) {
	s.logger.Info().Msg("hot-reloading artifact configuration")
	s.applyConfig(cfg)
	s.logger.Info().Msg("artifact configuration reloaded successfully")
}

// validate checks the source and target image references against the
// configured validation regexes. An empty regex disables validation.
func (s *artifactsrvc) validate(p *artifact.ImageSyncRequest) error {
	s.mu.RLock()
	src, dst := s.sourceRegx, s.targetRegx
	s.mu.RUnlock()

	if src != nil && !src.MatchString(p.Source) {
		return &artifact.HTTPError{
			Code:    http.StatusBadRequest,
			Message: fmt.Sprintf("source image not valid, must be %s", src.String()),
		}
	}
	if dst != nil && !dst.MatchString(p.Target) {
		return &artifact.HTTPError{
			Code:    http.StatusBadRequest,
			Message: fmt.Sprintf("target image not valid, must be %s", dst.String()),
		}
	}
	return nil
}

// SyncImage creates an image sync task and enqueues it asynchronously.
//
// When running in k8s pod, it should use the service account that has Docker authentication
// configured and appended to its context.
//
// When debugging locally, it will use the default authentication stored in the
// Docker config.json file (~/.docker/config.json).
func (s *artifactsrvc) SyncImage(ctx context.Context, p *artifact.ImageSyncRequest) (res *artifact.ImageSyncTask, err error) {
	if err := s.validate(p); err != nil {
		return nil, err
	}

	create := s.db.ImageSyncTask.Create().
		SetSource(p.Source).
		SetTarget(p.Target).
		SetStatus(TaskStatusPending).
		SetCreatedAt(time.Now()).
		SetUpdatedAt(time.Now())
	if targetKey := imageTargetKey(p.Target); targetKey != "" {
		create.SetTargetKey(targetKey)
	}
	task, err := create.Save(ctx)
	if err != nil {
		s.logger.Err(err).
			Str("source", p.Source).
			Str("target", p.Target).
			Msg("Failed to create image sync task")

		return nil, &artifact.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to create image sync task: %v", err),
		}
	}

	s.logger.Info().
		Int("id", task.ID).
		Str("source", p.Source).
		Str("target", p.Target).
		Msg("Image sync task enqueued")

	return toImageSyncTask(task), nil
}

// GetImageSyncTask returns the status of an image sync task.
func (s *artifactsrvc) GetImageSyncTask(ctx context.Context, p *artifact.GetImageSyncTaskPayload) (res *artifact.ImageSyncTask, err error) {
	task, err := s.db.ImageSyncTask.Get(ctx, p.ID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, &artifact.HTTPError{
				Code:    http.StatusNotFound,
				Message: fmt.Sprintf("image sync task %d not found", p.ID),
			}
		}
		s.logger.Err(err).Int("id", p.ID).Msg("Failed to get image sync task")
		return nil, &artifact.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to get image sync task: %v", err),
		}
	}

	return toImageSyncTask(task), nil
}

// ListImageSyncTasks lists image sync tasks with pagination and status filter.
func (s *artifactsrvc) ListImageSyncTasks(ctx context.Context, p *artifact.ListImageSyncTasksPayload) (res []*artifact.ImageSyncTask, err error) {
	query := s.db.ImageSyncTask.Query().
		Offset(p.PageSize * (p.Page - 1)).
		Limit(p.PageSize)
	if p.Status != nil {
		query.Where(entimagesynctask.Status(*p.Status))
	}

	// Map camelCase sort values to Ent column names.
	sortColumnMap := map[string]string{
		"createdAt": entimagesynctask.FieldCreatedAt,
		"updatedAt": entimagesynctask.FieldUpdatedAt,
	}
	if col, ok := sortColumnMap[p.Sort]; ok {
		if p.Direction == "desc" {
			query.Order(ent.Desc(col))
		} else {
			query.Order(ent.Asc(col))
		}
	}

	tasks, err := query.All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("Failed to list image sync tasks")
		return nil, &artifact.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to list image sync tasks: %v", err),
		}
	}

	for _, task := range tasks {
		res = append(res, toImageSyncTask(task))
	}
	return res, nil
}

// toImageSyncTask converts an ent record to the Goa result type.
func toImageSyncTask(task *ent.ImageSyncTask) *artifact.ImageSyncTask {
	res := &artifact.ImageSyncTask{
		ID:     task.ID,
		Source: task.Source,
		Target: task.Target,
		Status: task.Status,
	}
	if task.ErrMsg != "" {
		res.ErrorMessage = &task.ErrMsg
	}
	if !task.CreatedAt.IsZero() {
		res.CreatedAt = task.CreatedAt.Format(time.RFC3339)
	}
	if !task.UpdatedAt.IsZero() {
		res.UpdatedAt = task.UpdatedAt.Format(time.RFC3339)
	}
	return res
}
