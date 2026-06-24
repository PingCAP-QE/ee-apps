package service

import (
	"context"
	"fmt"
	"time"

	"github.com/rs/zerolog/log"
	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektonclient "github.com/tektoncd/pipeline/pkg/client/clientset/versioned/typed/pipeline/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const (
	eventIDLabel = "triggers.tekton.dev/eventId"
)

// TektonReconcilerConfig holds configuration for the reconciler.
type TektonReconcilerConfig struct {
	// Enabled controls whether the reconciler runs.
	Enabled bool `yaml:"enabled" json:"enabled"`
	// Namespace is the Kubernetes namespace where Tekton PipelineRuns are created.
	Namespace string `yaml:"namespace" json:"namespace"`
	// Interval is how often the reconciler polls for stale builds.
	Interval time.Duration `yaml:"interval" json:"interval"`
	// StaleThreshold is the minimum age of a PROCESSING build before reconciliation.
	StaleThreshold time.Duration `yaml:"stale_threshold" json:"stale_threshold"`
}

// DefaultReconcilerConfig returns a config with sensible defaults.
func DefaultReconcilerConfig() TektonReconcilerConfig {
	return TektonReconcilerConfig{
		Enabled:        false,
		Namespace:      "ee-cd",
		Interval:       60 * time.Second,
		StaleThreshold: 5 * time.Minute,
	}
}

// PipelineRunLister is an interface for listing PipelineRuns (for testing).
type PipelineRunLister interface {
	List(ctx context.Context, opts metav1.ListOptions) (*tekton.PipelineRunList, error)
}

// TektonReconciler reconciles DevBuild records with Tekton PipelineRun status.
type TektonReconciler struct {
	repo      DevBuildRepository
	config    TektonReconcilerConfig
	k8sClient kubernetes.Interface
	prLister  PipelineRunLister
	now       func() time.Time
}

// NewTektonReconciler creates a new reconciler with in-cluster Kubernetes config.
func NewTektonReconciler(repo DevBuildRepository, config TektonReconcilerConfig) (*TektonReconciler, error) {
	if !config.Enabled {
		return &TektonReconciler{repo: repo, config: config, now: time.Now}, nil
	}

	k8sConfig, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("failed to get in-cluster config: %w", err)
	}

	k8sClient, err := kubernetes.NewForConfig(k8sConfig)
	if err != nil {
		return nil, fmt.Errorf("failed to create kubernetes client: %w", err)
	}

	tektonCS, err := tektonclient.NewForConfig(k8sConfig)
	if err != nil {
		return nil, fmt.Errorf("failed to create tekton client: %w", err)
	}

	return &TektonReconciler{
		repo:      repo,
		config:    config,
		k8sClient: k8sClient,
		prLister:  tektonCS.PipelineRuns(config.Namespace),
		now:       time.Now,
	}, nil
}

// NewTektonReconcilerWithClients creates a reconciler with injected clients (for testing).
func NewTektonReconcilerWithClients(repo DevBuildRepository, config TektonReconcilerConfig, k8sClient kubernetes.Interface, prLister PipelineRunLister) *TektonReconciler {
	return &TektonReconciler{
		repo:      repo,
		config:    config,
		k8sClient: k8sClient,
		prLister:  prLister,
		now:       time.Now,
	}
}

// Start begins the reconciler loop in a background goroutine.
// It returns immediately. The loop runs until the context is cancelled.
func (r *TektonReconciler) Start(ctx context.Context) {
	if !r.config.Enabled {
		log.Info().Msg("tekton reconciler disabled")
		return
	}

	go r.run(ctx)
	log.Info().
		Dur("interval", r.config.Interval).
		Dur("stale_threshold", r.config.StaleThreshold).
		Str("namespace", r.config.Namespace).
		Msg("tekton reconciler started")
}

func (r *TektonReconciler) run(ctx context.Context) {
	ticker := time.NewTicker(r.config.Interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("tekton reconciler stopping")
			return
		case <-ticker.C:
			r.reconcile(ctx)
		}
	}
}

func (r *TektonReconciler) reconcile(ctx context.Context) {
	// Query all DevBuilds in PROCESSING status
	builds, err := r.repo.List(ctx, DevBuildListOption{})
	if err != nil {
		log.Error().Err(err).Msg("failed to list devbuilds for reconciliation")
		return
	}

	now := r.now()
	for _, build := range builds {
		// Only process Tekton builds in PROCESSING status with an eventID
		if build.Spec.PipelineEngine != TektonEngine {
			continue
		}
		if build.Status.Status != BuildStatusProcessing {
			continue
		}
		if build.Status.TektonStatus == nil || build.Status.TektonStatus.EventID == "" {
			continue
		}

		// Check if the build is stale (older than threshold)
		age := now.Sub(build.Meta.UpdatedAt)
		if age < r.config.StaleThreshold {
			continue
		}

		r.reconcileBuild(ctx, build)
	}
}

func (r *TektonReconciler) reconcileBuild(ctx context.Context, build DevBuild) {
	eventID := build.Status.TektonStatus.EventID
	logger := log.With().
		Int("build_id", build.ID).
		Str("event_id", eventID).
		Str("namespace", r.config.Namespace).
		Logger()

	// Query PipelineRuns with the eventID label
	selector := labels.SelectorFromSet(labels.Set{
		eventIDLabel: eventID,
	})

	pipelineRuns, err := r.prLister.List(ctx, metav1.ListOptions{
		LabelSelector: selector.String(),
	})
	if err != nil {
		logger.Error().Err(err).Msg("failed to list pipelineruns")
		return
	}

	if len(pipelineRuns.Items) == 0 {
		logger.Warn().Msg("no pipelineruns found for eventID")
		return
	}

	// Process each PipelineRun and merge status
	updated := false
	for _, pr := range pipelineRuns.Items {
		pipeline := r.pipelineRunToTektonPipeline(pr)
		if pipeline == nil {
			continue
		}

		// Only update if the PipelineRun has completed
		if pipeline.Status == BuildStatusProcessing {
			continue
		}

		// Re-fetch to avoid race conditions
		latest, err := r.repo.Get(ctx, build.ID)
		if err != nil {
			logger.Error().Err(err).Msg("failed to re-fetch build")
			continue
		}

		if latest.Status.TektonStatus == nil {
			latest.Status.TektonStatus = &TektonStatus{}
		}

		// Merge pipeline status (idempotent)
		r.mergePipelineStatus(latest.Status.TektonStatus, *pipeline)

		// Recompute overall status
		computeTektonStatus(latest.Status.TektonStatus, &latest.Status)

		latest.Meta.UpdatedAt = r.now()
		_, err = r.repo.Update(ctx, build.ID, *latest)
		if err != nil {
			logger.Error().Err(err).Msg("failed to update build status")
			continue
		}

		updated = true
		logger.Info().
			Str("pipeline_name", pipeline.Name).
			Str("pipeline_status", pipeline.Status).
			Msg("reconciled build status")
	}

	if updated {
		logger.Info().Msg("build reconciled successfully")
	}
}

func (r *TektonReconciler) pipelineRunToTektonPipeline(pr tekton.PipelineRun) *TektonPipeline {
	// Determine status based on PipelineRun conditions
	status := BuildStatusProcessing
	if pr.Status.CompletionTime != nil {
		// Check the Succeeded condition
		for _, cond := range pr.Status.Conditions {
			if cond.Type == "Succeeded" {
				if cond.Status == "True" {
					status = BuildStatusSuccess
				} else {
					status = BuildStatusFailure
				}
				break
			}
		}
	}

	// Parse platform from params
	platform := ""
	for _, p := range pr.Spec.Params {
		if p.Name == "os" {
			platform = p.Value.StringVal
		}
		if p.Name == "arch" && platform != "" {
			platform = platform + "/" + p.Value.StringVal
		}
	}

	// Parse git hash
	gitHash := ""
	for _, p := range pr.Spec.Params {
		if p.Name == "git-revision" {
			v := p.Value.StringVal
			if len(v) == 40 {
				gitHash = v
			}
		}
	}

	var startAt, endAt *time.Time
	if pr.Status.StartTime != nil {
		startAt = &pr.Status.StartTime.Time
	}
	if pr.Status.CompletionTime != nil {
		endAt = &pr.Status.CompletionTime.Time
	}

	return &TektonPipeline{
		Name:     pr.Name,
		Platform: platform,
		GitHash:  gitHash,
		Status:   status,
		StartAt:  startAt,
		EndAt:    endAt,
	}
}

func (r *TektonReconciler) mergePipelineStatus(tekton *TektonStatus, pipeline TektonPipeline) {
	name := pipeline.Name
	index := -1
	for i, p := range tekton.Pipelines {
		if p.Name == name {
			index = i
		}
	}
	if index >= 0 {
		tekton.Pipelines[index] = pipeline
	} else {
		tekton.Pipelines = append(tekton.Pipelines, pipeline)
	}
}
