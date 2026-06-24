package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/rs/zerolog"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektonclient "github.com/tektoncd/pipeline/pkg/client/clientset/versioned"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"
	"knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
)

const (
	// staleThreshold is the minimum duration a build must be in PROCESSING state
	// before the reconciler attempts to sync its status.
	staleThreshold = 5 * time.Minute

	// reconcileInterval is the interval at which the reconciler runs.
	reconcileInterval = 30 * time.Second
)

// TektonReconciler periodically checks for builds stuck in PROCESSING state
// and syncs their status with the actual PipelineRun status from Kubernetes.
type TektonReconciler struct {
	logger       *zerolog.Logger
	dbClient     *ent.Client
	tektonClient tektonclient.Interface
	namespace    string
}

// NewTektonReconciler creates a new TektonReconciler.
func NewTektonReconciler(logger *zerolog.Logger, dbClient *ent.Client, namespace string) (*TektonReconciler, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("failed to get in-cluster config: %w", err)
	}

	tektonClient, err := tektonclient.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create tekton client: %w", err)
	}

	return &TektonReconciler{
		logger:       logger,
		dbClient:     dbClient,
		tektonClient: tektonClient,
		namespace:    namespace,
	}, nil
}

// Start begins the reconciliation loop. It runs until the context is cancelled.
func (r *TektonReconciler) Start(ctx context.Context) {
	r.logger.Info().Msg("starting tekton reconciler")

	ticker := time.NewTicker(reconcileInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			r.logger.Info().Msg("stopping tekton reconciler")
			return
		case <-ticker.C:
			if err := r.reconcile(ctx); err != nil {
				r.logger.Err(err).Msg("failed to reconcile")
			}
		}
	}
}

// reconcile checks for stale PROCESSING builds and syncs their status.
func (r *TektonReconciler) reconcile(ctx context.Context) error {
	r.logger.Debug().Msg("running reconciliation cycle")

	// Query builds that are in PROCESSING state and have been there for >5 minutes
	threshold := time.Now().Add(-staleThreshold)
	builds, err := r.dbClient.DevBuild.Query().
		Where(
			entdevbuild.Status("processing"),
			entdevbuild.UpdatedAtLT(threshold),
		).
		All(ctx)
	if err != nil {
		return fmt.Errorf("failed to query stale builds: %w", err)
	}

	if len(builds) == 0 {
		r.logger.Debug().Msg("no stale builds found")
		return nil
	}

	r.logger.Info().Int("count", len(builds)).Msg("found stale builds to reconcile")

	for _, build := range builds {
		if err := r.reconcileBuild(ctx, build); err != nil {
			r.logger.Err(err).Int("build_id", build.ID).Msg("failed to reconcile build")
			// Continue with other builds even if one fails
			continue
		}
	}

	return nil
}

// reconcileBuild syncs a single build's status with its PipelineRun status.
func (r *TektonReconciler) reconcileBuild(ctx context.Context, build *ent.DevBuild) error {
	logger := r.logger.With().Int("build_id", build.ID).Logger()

	// Extract event IDs from tekton_status
	eventIDs := r.extractEventIDs(build.TektonStatus)
	if len(eventIDs) == 0 {
		logger.Debug().Msg("no event IDs found, skipping")
		return nil
	}

	// Query PipelineRuns for each event ID
	for _, eventID := range eventIDs {
		pipelineRuns, err := r.listPipelineRunsByEventID(ctx, eventID)
		if err != nil {
			logger.Err(err).Str("event_id", eventID).Msg("failed to list pipeline runs")
			continue
		}

		if len(pipelineRuns) == 0 {
			logger.Debug().Str("event_id", eventID).Msg("no pipeline runs found for event ID")
			continue
		}

		// Update build status based on PipelineRun status
		if err := r.updateBuildFromPipelineRuns(ctx, build, pipelineRuns); err != nil {
			logger.Err(err).Str("event_id", eventID).Msg("failed to update build from pipeline runs")
			continue
		}
	}

	return nil
}

// extractEventIDs extracts event IDs from the tekton_status JSON field.
func (r *TektonReconciler) extractEventIDs(tektonStatus map[string]any) []string {
	if tektonStatus == nil {
		return nil
	}

	// Check for triggers_event_ids field (added in PR1)
	if eventIDsRaw, ok := tektonStatus["triggers_event_ids"]; ok {
		if eventIDsJSON, ok := eventIDsRaw.(json.RawMessage); ok {
			var eventIDs []string
			if err := json.Unmarshal(eventIDsJSON, &eventIDs); err == nil {
				return eventIDs
			}
		}
		// Try direct string slice
		if eventIDs, ok := eventIDsRaw.([]string); ok {
			return eventIDs
		}
		// Try []any
		if eventIDsAny, ok := eventIDsRaw.([]any); ok {
			var result []string
			for _, id := range eventIDsAny {
				if idStr, ok := id.(string); ok {
					result = append(result, idStr)
				}
			}
			return result
		}
	}

	return nil
}

// listPipelineRunsByEventID lists PipelineRuns that match the given event ID label.
func (r *TektonReconciler) listPipelineRunsByEventID(ctx context.Context, eventID string) ([]v1beta1.PipelineRun, error) {
	// Query PipelineRuns with the event ID label
	labelSelector := fmt.Sprintf("tekton.dev/eventID=%s", eventID)
	pipelineRuns, err := r.tektonClient.TektonV1beta1().PipelineRuns(r.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labelSelector,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to list pipeline runs: %w", err)
	}

	return pipelineRuns.Items, nil
}

// updateBuildFromPipelineRuns updates a build's status based on PipelineRun status.
func (r *TektonReconciler) updateBuildFromPipelineRuns(ctx context.Context, build *ent.DevBuild, pipelineRuns []v1beta1.PipelineRun) error {
	if len(pipelineRuns) == 0 {
		return nil
	}

	// Determine overall status from PipelineRuns
	newStatus := r.determineStatus(pipelineRuns)
	if newStatus == "" || newStatus == build.Status {
		return nil
	}

	logger := r.logger.With().
		Int("build_id", build.ID).
		Str("old_status", build.Status).
		Str("new_status", newStatus).
		Logger()

	logger.Info().Msg("updating build status from pipeline run")

	// Update the build status
	updater := r.dbClient.DevBuild.UpdateOneID(build.ID).
		SetStatus(newStatus).
		SetUpdatedAt(time.Now())

	// Update tekton status with pipeline info
	tektonStatus := r.buildTektonStatus(pipelineRuns, build.TektonStatus)
	if tektonStatus != nil {
		updater.SetTektonStatus(tektonStatus)
	}

	// Set pipeline times
	if startTime := r.getEarliestStartTime(pipelineRuns); startTime != nil {
		updater.SetPipelineStartAt(*startTime)
	}
	if endTime := r.getLatestCompletionTime(pipelineRuns); endTime != nil {
		updater.SetPipelineEndAt(*endTime)
	}

	if _, err := updater.Save(ctx); err != nil {
		return fmt.Errorf("failed to update build: %w", err)
	}

	return nil
}

// determineStatus determines the overall build status from PipelineRuns.
func (r *TektonReconciler) determineStatus(pipelineRuns []v1beta1.PipelineRun) string {
	hasFailure := false
	hasRunning := false
	hasSuccess := false

	for _, pr := range pipelineRuns {
		condition := pr.Status.GetCondition(apis.ConditionSucceeded)
		if condition == nil {
			hasRunning = true
			continue
		}

		switch {
		case condition.IsTrue():
			hasSuccess = true
		case condition.IsFalse():
			hasFailure = true
		default:
			hasRunning = true
		}
	}

	if hasFailure {
		return "failure"
	}
	if hasRunning {
		return "processing"
	}
	if hasSuccess {
		return "success"
	}

	return ""
}

// buildTektonStatus builds the tekton_status JSON from PipelineRuns.
func (r *TektonReconciler) buildTektonStatus(pipelineRuns []v1beta1.PipelineRun, existing map[string]any) map[string]any {
	if existing == nil {
		existing = make(map[string]any)
	}

	var pipelines []map[string]any
	for _, pr := range pipelineRuns {
		pipeline := map[string]any{
			"name": pr.Name,
		}

		condition := pr.Status.GetCondition(apis.ConditionSucceeded)
		if condition != nil {
			if condition.IsTrue() {
				pipeline["status"] = "success"
			} else if condition.IsFalse() {
				pipeline["status"] = "failure"
			} else {
				pipeline["status"] = "processing"
			}
		}

		if pr.Status.StartTime != nil {
			pipeline["start_at"] = pr.Status.StartTime.Time.Format(time.RFC3339)
		}
		if pr.Status.CompletionTime != nil {
			pipeline["end_at"] = pr.Status.CompletionTime.Time.Format(time.RFC3339)
		}

		// Extract platform from labels or params
		if platform, ok := pr.Labels["tekton.dev/platform"]; ok {
			pipeline["platform"] = platform
		}

		pipelines = append(pipelines, pipeline)
	}

	existing["pipelines"] = pipelines
	return existing
}

// getEarliestStartTime gets the earliest start time from PipelineRuns.
func (r *TektonReconciler) getEarliestStartTime(pipelineRuns []v1beta1.PipelineRun) *time.Time {
	var earliest *time.Time
	for _, pr := range pipelineRuns {
		if pr.Status.StartTime != nil {
			t := pr.Status.StartTime.Time
			if earliest == nil || t.Before(*earliest) {
				earliest = &t
			}
		}
	}
	return earliest
}

// getLatestCompletionTime gets the latest completion time from PipelineRuns.
func (r *TektonReconciler) getLatestCompletionTime(pipelineRuns []v1beta1.PipelineRun) *time.Time {
	var latest *time.Time
	for _, pr := range pipelineRuns {
		if pr.Status.CompletionTime != nil {
			t := pr.Status.CompletionTime.Time
			if latest == nil || t.After(*latest) {
				latest = &t
			}
		}
	}
	return latest
}

// parsePipelineName extracts the pipeline name from the event source.
func parsePipelineName(source string) string {
	// Source format: /apis/namespaces/<namespace>/<run-name>
	parts := strings.Split(source, "/")
	if len(parts) >= 2 {
		return parts[len(parts)-1]
	}
	return ""
}
