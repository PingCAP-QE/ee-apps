package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

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

// PipelineRunListResponse represents the response from Tekton Dashboard API.
type PipelineRunListResponse struct {
	Items []PipelineRunItem `json:"items"`
}

// PipelineRunItem represents a single PipelineRun from the Dashboard API.
type PipelineRunItem struct {
	Metadata struct {
		Name   string            `json:"name"`
		Labels map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		Params []Param `json:"params"`
	} `json:"spec"`
	Status struct {
		Conditions      []Condition `json:"conditions"`
		StartTime       string      `json:"startTime"`
		CompletionTime  string      `json:"completionTime"`
		PipelineResults []Result    `json:"pipelineResults"`
	} `json:"status"`
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
func (s *devbuildsrvc) queryPipelineRuns(ctx context.Context, eventID string) ([]PipelineRunItem, error) {
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

	var pipelineRunList PipelineRunListResponse
	if err := json.NewDecoder(resp.Body).Decode(&pipelineRunList); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	return pipelineRunList.Items, nil
}

// updateBuildFromPipelineRuns updates a build's status based on PipelineRun status.
func (s *devbuildsrvc) updateBuildFromPipelineRuns(ctx context.Context, build *ent.DevBuild, pipelineRuns []PipelineRunItem) error {
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
		updater.SetPipelineStartAt(*startTime)
	}
	if endTime := getLatestCompletionTime(pipelineRuns); endTime != nil {
		updater.SetPipelineEndAt(*endTime)
	}

	if _, err := updater.Save(ctx); err != nil {
		return fmt.Errorf("failed to update build: %w", err)
	}

	return nil
}

// determineStatus determines the overall build status from PipelineRuns.
func determineStatus(pipelineRuns []PipelineRunItem) string {
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
func getSucceededCondition(pr PipelineRunItem) *Condition {
	for _, cond := range pr.Status.Conditions {
		if cond.Type == "Succeeded" {
			return &cond
		}
	}
	return nil
}

// buildTektonStatus builds the tekton_status from PipelineRuns.
func buildTektonStatus(pipelineRuns []PipelineRunItem, existing schema.TektonStatus) schema.TektonStatus {
	var pipelines []schema.TektonPipeline
	for _, pr := range pipelineRuns {
		pipeline := schema.TektonPipeline{
			Name: pr.Metadata.Name,
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

		if pr.Status.StartTime != "" {
			pipeline.StartAt = pr.Status.StartTime
		}
		if pr.Status.CompletionTime != "" {
			pipeline.EndAt = pr.Status.CompletionTime
		}

		// Extract platform from labels
		if platform, ok := pr.Metadata.Labels["tekton.dev/platform"]; ok {
			pipeline.Platform = platform
		}

		// Extract results (OCI artifacts + images)
		ociArtifacts, images := extractArtifactsFromResults(pr.Status.PipelineResults)
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
func extractArtifactsFromResults(results []Result) (ociArtifacts []schema.OciArtifact, images []schema.ImageArtifact) {
	for _, result := range results {
		switch result.Name {
		case "pushed-binaries":
			var artifacts []schema.OciArtifact
			if err := json.Unmarshal([]byte(result.Value), &artifacts); err == nil {
				ociArtifacts = append(ociArtifacts, artifacts...)
			}
		case "pushed-images":
			var imgs []schema.ImageArtifact
			if err := json.Unmarshal([]byte(result.Value), &imgs); err == nil {
				images = append(images, imgs...)
			}
		}
	}
	return
}

// getEarliestStartTime gets the earliest start time from PipelineRuns.
func getEarliestStartTime(pipelineRuns []PipelineRunItem) *time.Time {
	var earliest *time.Time
	for _, pr := range pipelineRuns {
		if pr.Status.StartTime != "" {
			t, err := time.Parse(time.RFC3339, pr.Status.StartTime)
			if err != nil {
				continue
			}
			if earliest == nil || t.Before(*earliest) {
				earliest = &t
			}
		}
	}
	return earliest
}

// getLatestCompletionTime gets the latest completion time from PipelineRuns.
func getLatestCompletionTime(pipelineRuns []PipelineRunItem) *time.Time {
	var latest *time.Time
	for _, pr := range pipelineRuns {
		if pr.Status.CompletionTime != "" {
			t, err := time.Parse(time.RFC3339, pr.Status.CompletionTime)
			if err != nil {
				continue
			}
			if latest == nil || t.After(*latest) {
				latest = &t
			}
		}
	}
	return latest
}
