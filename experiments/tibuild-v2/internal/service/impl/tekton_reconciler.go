package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	knative "knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
)

// terminalStatuses are the build statuses that indicate a build is finished.
var terminalStatuses = []string{"success", "failure", "error", "aborted"}

// Reconcile checks for non-terminal builds and syncs their status with Tekton Dashboard API.
func (s *devbuildsrvc) Reconcile(ctx context.Context) {
	s.logger.Debug().Msg("running reconciliation cycle")

	// Query all non-terminal builds
	builds, err := s.dbClient.DevBuild.Query().
		Where(entdevbuild.StatusNotIn(terminalStatuses...)).
		All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to query non-terminal builds")
		return
	}

	if len(builds) == 0 {
		s.logger.Debug().Msg("no non-terminal builds found")
		return
	}

	s.logger.Info().Int("count", len(builds)).Msg("found non-terminal builds to reconcile")

	for _, build := range builds {
		if err := s.reconcileBuild(ctx, build); err != nil {
			s.logger.Err(err).Int("build_id", build.ID).Msg("failed to reconcile build")
			continue
		}
	}
}

// reconcileBuild syncs a single build's status with its PipelineRun status.
func (s *devbuildsrvc) reconcileBuild(ctx context.Context, build *ent.DevBuild) error {
	logger := s.logger.With().Int("build_id", build.ID).Logger()

	// Extract event IDs from tekton_status
	eventIDs := build.TektonStatus.TriggersEventIds
	if len(eventIDs) == 0 {
		logger.Debug().Msg("no event IDs found, skipping")
		return nil
	}

	// Query PipelineRuns for each event ID
	for _, eventID := range eventIDs {
		pipelineRuns, err := s.queryPipelineRuns(ctx, eventID)
		if err != nil {
			logger.Err(err).Str("event_id", eventID).Msg("failed to query pipeline runs")
			continue
		}

		if len(pipelineRuns) == 0 {
			logger.Debug().Str("event_id", eventID).Msg("no pipeline runs found for event ID")
			continue
		}

		// Update build status based on PipelineRun status
		if err := s.updateBuildFromPipelineRuns(ctx, build, pipelineRuns); err != nil {
			logger.Err(err).Str("event_id", eventID).Msg("failed to update build from pipeline runs")
			continue
		}
	}

	return nil
}

// Param represents a PipelineRun parameter.
type Param struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Condition represents a status condition.
type Condition struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason"`
	Message string `json:"message"`
}

// Result represents a PipelineRun result.
type Result struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// queryPipelineRuns queries PipelineRuns from Tekton Dashboard API by event ID.
func (s *devbuildsrvc) queryPipelineRuns(ctx context.Context, eventID string) ([]tknv1.PipelineRun, error) {
	labelSelector := fmt.Sprintf("triggers.tekton.dev/triggers-eventid=%s", eventID)
	apiURL := fmt.Sprintf("%s/apis/tekton.dev/v1/pipelineruns/?labelSelector=%s",
		s.dashboardURL, url.QueryEscape(labelSelector))

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to execute request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("unexpected status code: %d, body: %s", resp.StatusCode, string(body))
	}

	var pipelineRunList tknv1.PipelineRunList
	if err := json.NewDecoder(resp.Body).Decode(&pipelineRunList); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	return pipelineRunList.Items, nil
}

// updateBuildFromPipelineRuns updates a build's status based on PipelineRun status.
func (s *devbuildsrvc) updateBuildFromPipelineRuns(ctx context.Context, build *ent.DevBuild, pipelineRuns []tknv1.PipelineRun) error {
	if len(pipelineRuns) == 0 {
		return nil
	}

	// Determine overall status from PipelineRuns
	newStatus := determineStatus(pipelineRuns)
	if newStatus == "" || newStatus == build.Status {
		return nil
	}

	logger := s.logger.With().
		Int("build_id", build.ID).
		Str("old_status", build.Status).
		Str("new_status", newStatus).
		Logger()

	logger.Info().Msg("updating build status from pipeline run")

	// Update the build status
	updater := s.dbClient.DevBuild.UpdateOneID(build.ID).
		SetStatus(newStatus).
		SetUpdatedAt(time.Now())

	// Update tekton status with pipeline info
	tektonStatus := buildTektonStatus(pipelineRuns, build.TektonStatus)
	updater.SetTektonStatus(tektonStatus)

	// Set pipeline times
	if startTime := getEarliestStartTime(pipelineRuns); startTime != nil {
		updater.SetPipelineStartAt(startTime.Time)
	}
	if endTime := getLatestCompletionTime(pipelineRuns); endTime != nil {
		updater.SetPipelineEndAt(endTime.Time)
	}

	if _, err := updater.Save(ctx); err != nil {
		return fmt.Errorf("failed to update build: %w", err)
	}

	return nil
}

// determineStatus determines the overall build status from PipelineRuns.
func determineStatus(pipelineRuns []tknv1.PipelineRun) string {
	hasFailure := false
	hasRunning := false
	hasSuccess := false

	for _, pr := range pipelineRuns {
		condition := getSucceededCondition(pr)
		if condition == nil {
			hasRunning = true
			continue
		}

		switch condition.Status {
		case "True":
			hasSuccess = true
		case "False":
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

// getSucceededCondition gets the Succeeded condition from a PipelineRun.
func getSucceededCondition(pr tknv1.PipelineRun) *knative.Condition {
	for _, cond := range pr.Status.Conditions {
		if cond.Type == knative.ConditionSucceeded {
			return &cond
		}
	}
	return nil
}

// buildTektonStatus builds the tekton_status from PipelineRuns.
func buildTektonStatus(pipelineRuns []tknv1.PipelineRun, existing schema.TektonStatus) schema.TektonStatus {
	var pipelines []schema.TektonPipeline
	for _, pr := range pipelineRuns {
		pipeline := schema.TektonPipeline{
			Name:      pr.GetName(),
			Namespace: pr.GetNamespace(),
		}

		condition := getSucceededCondition(pr)
		if condition != nil {
			switch condition.Status {
			case "True":
				pipeline.Status = "success"
			case "False":
				pipeline.Status = "failure"
			default:
				pipeline.Status = "processing"
			}
		}

		if pr.Status.StartTime != nil {
			pipeline.StartAt = &pr.Status.StartTime.Time
		}
		if pr.Status.CompletionTime != nil {
			pipeline.EndAt = &pr.Status.CompletionTime.Time
		}

		// Extract platform from labels
		if platform, ok := pr.GetLabels()["tekton.dev/platform"]; ok {
			pipeline.Platform = platform
		}

		// Extract results (OCI artifacts + images)
		ociArtifacts, images := extractArtifactsFromResults(pr.Status.Results)
		if len(ociArtifacts) > 0 {
			pipeline.OciArtifacts = ociArtifacts
		}
		if len(images) > 0 {
			pipeline.Images = images
		}

		pipelines = append(pipelines, pipeline)
	}

	existing.Pipelines = pipelines
	return existing
}

// extractArtifactsFromResults extracts OCI artifacts and images from PipelineRun results.
func extractArtifactsFromResults(results []tknv1.PipelineRunResult) (ociArtifacts []schema.OciArtifact, images []schema.ImageArtifact) {
	for _, result := range results {
		switch result.Name {
		case "pushed-binaries":
			var artifacts []schema.OciArtifact
			vbs, err := result.Value.MarshalJSON()
			if err != nil {
				continue
			}
			if err := json.Unmarshal(vbs, &artifacts); err == nil {
				ociArtifacts = append(ociArtifacts, artifacts...)
			}
		case "pushed-images":
			var imgs []schema.ImageArtifact
			vbs, err := result.Value.MarshalJSON()
			if err != nil {
				continue
			}
			if err := json.Unmarshal(vbs, &imgs); err == nil {
				images = append(images, imgs...)
			}
		}
	}
	return
}

// getEarliestStartTime gets the earliest start time from PipelineRuns.
func getEarliestStartTime(pipelineRuns []tknv1.PipelineRun) *metav1.Time {
	var earliest *metav1.Time
	for _, pr := range pipelineRuns {
		if pr.Status.StartTime != nil {
			t := pr.Status.StartTime
			if earliest == nil || t.Before(earliest) {
				earliest = t
			}
		}
	}
	return earliest
}

// getLatestCompletionTime gets the latest completion time from PipelineRuns.
func getLatestCompletionTime(pipelineRuns []tknv1.PipelineRun) *metav1.Time {
	var latest *metav1.Time
	for _, pr := range pipelineRuns {
		if pr.Status.CompletionTime != nil {
			t := pr.Status.CompletionTime
			if latest == nil || t.After(latest.Time) {
				latest = t
			}
		}
	}
	return latest
}
