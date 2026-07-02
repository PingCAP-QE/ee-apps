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
	yaml "gopkg.in/yaml.v3"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	knative "knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
)

// terminalStatuses are the build statuses that indicate a build is finished.
var terminalStatuses = []string{"SUCCESS", "FAILURE", "ERROR", "ABORTED"}

type (
	// buildReconcileData holds the data needed to reconcile a single build.
	buildReconcileData struct {
		Build        *ent.DevBuild
		PipelineRuns []tknv1.PipelineRun
	}

	// YAML format types for parsing PipelineRun result values.
	binariesResultYAML struct {
		Oci   *ociRefYAML `yaml:"oci"`
		Files []string    `yaml:"files"`
	}
	ociRefYAML struct {
		Repo string `yaml:"repo"`
		Tag  string `yaml:"tag"`
	}
	imagesResultYAML struct {
		Images []imageEntryYAML `yaml:"images"`
	}
	imageEntryYAML struct {
		Repo string `yaml:"repo"`
		Tag  string `yaml:"tag"`
	}
)

// Reconcile checks for non-terminal builds and syncs their status with Tekton Dashboard API.
// It separates Tekton HTTP calls (slow) from DB updates (fast) to minimize lock contention.
func (s *devbuildsrvc) Reconcile(ctx context.Context) {
	s.logger.Debug().Msg("running reconciliation cycle")

	// Query non-terminal builds created within the configured lookback period.
	builds, err := s.dbClient.DevBuild.Query().
		Where(
			entdevbuild.StatusNotIn(terminalStatuses...),
			entdevbuild.CreatedAtGTE(time.Now().Add(-s.reconcilerSince)),
		).
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

	// Phase 1: Fetch Tekton PipelineRuns for all builds (slow HTTP calls, no DB locks).
	var pending []buildReconcileData
	for _, build := range builds {
		eventIDs := build.TektonStatus.TriggersEventIds
		if len(eventIDs) == 0 {
			continue
		}

		var allRuns []tknv1.PipelineRun
		for _, eventID := range eventIDs {
			runs, err := s.queryPipelineRuns(ctx, eventID)
			if err != nil {
				s.logger.Err(err).Int("build_id", build.ID).Str("event_id", eventID).Msg("failed to query pipeline runs")
				continue
			}
			allRuns = append(allRuns, runs...)
		}

		if len(allRuns) > 0 {
			pending = append(pending, buildReconcileData{Build: build, PipelineRuns: allRuns})
		}
	}

	// Phase 2: Update DB for builds that have PipelineRun data (fast, minimal lock time).
	for _, data := range pending {
		if err := s.updateBuildFromPipelineRuns(ctx, data.Build, data.PipelineRuns); err != nil {
			s.logger.Err(err).Int("build_id", data.Build.ID).Msg("failed to reconcile build")
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
	tektonStatus := buildTektonStatus(pipelineRuns, build.TektonStatus, s.dashboardURL)
	updater.SetTektonStatus(tektonStatus)

	// Build report from pipeline runs
	buildReport := buildBuildReport(pipelineRuns)
	if buildReport != nil {
		updater.SetBuildReport(*buildReport)
	}

	// Set pipeline times
	if startTime := getEarliestStartTime(pipelineRuns); startTime != nil {
		updater.SetPipelineStartAt(startTime.Time)
	}
	if endTime := getLatestCompletionTime(pipelineRuns); endTime != nil {
		updater.SetPipelineEndAt(endTime.Time)
	}

	if err := updater.Exec(ctx); err != nil {
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
		return "FAILURE"
	}
	if hasRunning {
		return "PROCESSING"
	}
	if hasSuccess {
		return "SUCCESS"
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
func buildTektonStatus(pipelineRuns []tknv1.PipelineRun, existing schema.TektonStatus, dashboardURL string) schema.TektonStatus {
	var pipelines []schema.TektonPipeline
	for _, pr := range pipelineRuns {
		pipeline := schema.TektonPipeline{
			Name:      pr.GetName(),
			Namespace: pr.GetNamespace(),
		}

		condition := getSucceededCondition(pr)
		if condition != nil {
			// Use the more informative Reason if available (e.g., "Succeeded",
			// "Failed", "Cancelled", "TimedOut", "Skipped", "ExceededNodeResources").
			// Fall back to Status (True/False/Unknown) when Reason is empty.
			if condition.Reason != "" {
				pipeline.Status = condition.Reason
			} else {
				pipeline.Status = string(condition.Status)
			}
		}

		if pr.Status.StartTime != nil {
			pipeline.StartAt = &pr.Status.StartTime.Time
		}
		if pr.Status.CompletionTime != nil {
			pipeline.EndAt = &pr.Status.CompletionTime.Time
		}

		// Extract platform from params (os + arch)
		pipeline.Platform = parsePlatformFromParams(pr.Spec.Params)

		// Build dashboard view URL
		if dashboardURL != "" {
			pipeline.URL = fmt.Sprintf("%s/#/namespaces/%s/pipelineruns/%s",
				dashboardURL, pr.GetNamespace(), pr.GetName())
		}

		// Extract results (OCI artifacts + images)
		ociArtifacts, images := extractArtifactsFromResults(pr.Status.Results, pr.Spec.Params)
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

// parsePlatformFromParams extracts the platform string (os/arch) from PipelineRun params.
func parsePlatformFromParams(params tknv1.Params) string {
	osVal, archVal := "", ""
	for _, p := range params {
		switch p.Name {
		case "os":
			osVal = p.Value.StringVal
		case "arch":
			archVal = p.Value.StringVal
		}
	}
	if osVal != "" && archVal != "" {
		return osVal + "/" + archVal
	}
	return ""
}

// extractArtifactsFromResults extracts OCI artifacts and images from PipelineRun results.
// The result values are YAML-formatted strings, not JSON.
func extractArtifactsFromResults(results []tknv1.PipelineRunResult, params tknv1.Params) (ociArtifacts []schema.OciArtifact, images []schema.ImageArtifact) {
	for _, result := range results {
		switch result.Name {
		case "pushed-binaries":
			var bin binariesResultYAML
			if err := yaml.Unmarshal([]byte(result.Value.StringVal), &bin); err == nil && bin.Oci != nil {
				ociArtifacts = append(ociArtifacts, schema.OciArtifact{
					Repo:  bin.Oci.Repo,
					Tag:   bin.Oci.Tag,
					Files: bin.Files,
				})
			}
		case "pushed-images":
			var imgs imagesResultYAML
			if err := yaml.Unmarshal([]byte(result.Value.StringVal), &imgs); err == nil {
				platform := parsePlatformFromParams(params)
				for _, img := range imgs.Images {
					images = append(images, schema.ImageArtifact{
						Platform: platform,
						URL:      img.Repo + ":" + img.Tag,
					})
				}
			}
		}
	}
	return
}

// buildBuildReport builds a BuildReport from PipelineRun params and results.
func buildBuildReport(pipelineRuns []tknv1.PipelineRun) *schema.BuildReport {
	report := &schema.BuildReport{}
	hasData := false

	for _, pr := range pipelineRuns {
		// Extract git-revision param
		for _, p := range pr.Spec.Params {
			if p.Name == "git-revision" && len(p.Value.StringVal) == 40 {
				if report.GitHash == "" {
					report.GitHash = p.Value.StringVal
					hasData = true
				}
			}
		}

		// Extract results (YAML-formatted strings)
		for _, r := range pr.Status.Results {
			switch r.Name {
			case "pushed-images":
				var imgs imagesResultYAML
				if err := yaml.Unmarshal([]byte(r.Value.StringVal), &imgs); err == nil {
					platform := parsePlatformFromParams(pr.Spec.Params)
					for _, img := range imgs.Images {
						report.Images = append(report.Images, schema.ImageArtifact{
							Platform: platform,
							URL:      img.Repo + ":" + img.Tag,
						})
						hasData = true
					}
				}
			case "pushed-binaries":
				var bin binariesResultYAML
				if err := yaml.Unmarshal([]byte(r.Value.StringVal), &bin); err == nil && bin.Oci != nil {
					report.Binaries = append(report.Binaries, schema.OciArtifact{
						Repo:  bin.Oci.Repo,
						Tag:   bin.Oci.Tag,
						Files: bin.Files,
					})
					hasData = true
				}
			case "printed-version":
				if report.PrintedVersion == "" {
					report.PrintedVersion = r.Value.StringVal
					hasData = true
				}
			case "plugin-git-sha":
				if report.PluginGitHash == "" {
					report.PluginGitHash = r.Value.StringVal
					hasData = true
				}
			}
		}
	}

	if !hasData {
		return nil
	}
	return report
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
