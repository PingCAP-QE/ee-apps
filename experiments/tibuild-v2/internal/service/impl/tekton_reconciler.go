package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"reflect"
	"strings"
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
		Build          *ent.DevBuild
		PipelineRuns   []tknv1.PipelineRun
		ReleaseVersion string
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
		Repo          string   `yaml:"repo"`
		Tag           string   `yaml:"tag"`
		MultiArchTags []string `yaml:"multi_arch_tags"`
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
			data := buildReconcileData{Build: build, PipelineRuns: allRuns}
			if isServerlessProduct(build.Product) {
				version, err := s.queryReleaseVersion(ctx, allRuns)
				if err != nil {
					s.logger.Err(err).Int("build_id", build.ID).Msg("failed to query release version")
				}
				data.ReleaseVersion = version
			}
			pending = append(pending, data)
		}
	}

	// Phase 2: Update DB for builds that have PipelineRun data (fast, minimal lock time).
	for _, data := range pending {
		if err := s.updateBuildFromPipelineRuns(ctx, data.Build, data.PipelineRuns, data.ReleaseVersion); err != nil {
			s.logger.Err(err).Int("build_id", data.Build.ID).Msg("failed to reconcile build")
		}
	}
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

// queryReleaseVersion reads the existing version Task result without extending Pipeline results.
func (s *devbuildsrvc) queryReleaseVersion(ctx context.Context, runs []tknv1.PipelineRun) (string, error) {
	names := make([]string, 0, len(runs))
	for _, run := range runs {
		names = append(names, run.Name)
	}
	selector := "tekton.dev/pipelineTask=get-release-ver,tekton.dev/pipelineRun in (" + strings.Join(names, ",") + ")"
	apiURL := fmt.Sprintf("%s/apis/tekton.dev/v1/taskruns/?labelSelector=%s", s.dashboardURL, url.QueryEscape(selector))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL, nil)
	if err != nil {
		return "", err
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("version TaskRun query returned HTTP %d", resp.StatusCode)
	}
	var tasks tknv1.TaskRunList
	if err := json.NewDecoder(resp.Body).Decode(&tasks); err != nil {
		return "", err
	}
	version := ""
	for _, task := range tasks.Items {
		for _, result := range task.Status.Results {
			if result.Name != "version" || result.Value.StringVal == "" {
				continue
			}
			if version != "" && version != result.Value.StringVal {
				return "", fmt.Errorf("platform version mismatch: %s != %s", version, result.Value.StringVal)
			}
			version = result.Value.StringVal
		}
	}
	return version, nil
}

// updateBuildFromPipelineRuns updates a build's status based on PipelineRun status.
func (s *devbuildsrvc) updateBuildFromPipelineRuns(ctx context.Context, build *ent.DevBuild, pipelineRuns []tknv1.PipelineRun, releaseVersion string) error {
	if len(pipelineRuns) == 0 {
		return nil
	}

	// Determine overall status from PipelineRuns
	newStatus := determineBuildStatus(build, pipelineRuns)
	// Wait for the existing version Task result while still refreshing artifact reports.
	if isServerlessProduct(build.Product) && newStatus == "SUCCESS" && releaseVersion == "" {
		newStatus = "PROCESSING"
	}
	tektonStatus := buildTektonStatus(pipelineRuns, build.TektonStatus, s.dashboardURL)
	buildReport := buildBuildReport(pipelineRuns)
	if releaseVersion != "" && buildReport != nil {
		buildReport.PrintedVersion = releaseVersion
	}
	if newStatus == "" || (newStatus == build.Status && reflect.DeepEqual(tektonStatus, build.TektonStatus) && (buildReport == nil || reflect.DeepEqual(*buildReport, build.BuildReport))) {
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
	updater.SetTektonStatus(tektonStatus)

	// Build report from pipeline runs
	if buildReport != nil {
		updater.SetBuildReport(*buildReport)
	}

	// Set pipeline times
	if startTime := getEarliestStartTime(pipelineRuns); startTime != nil {
		updater.SetPipelineStartAt(startTime.Time)
	}
	if endTime := getLatestCompletionTime(pipelineRuns); endTime != nil && isTerminalStatus(newStatus) {
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
					Repo:     bin.Oci.Repo,
					Tag:      bin.Oci.Tag,
					Files:    bin.Files,
					Platform: parsePlatformFromParams(params),
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
					// If the image has multi-arch tags, append the multi-arch images
					// for every tag (manifest list, no platform suffix).
					for _, tag := range img.MultiArchTags {
						images = append(images, schema.ImageArtifact{
							Platform: MultiArch,
							URL:      img.Repo + ":" + tag,
						})
					}
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
						report.Images = appendUniqueImage(report.Images, schema.ImageArtifact{
							Platform: platform,
							URL:      img.Repo + ":" + img.Tag,
						})
						hasData = true
						// If the image has multi-arch tags, append the multi-arch images
						// for every tag (manifest list, no platform suffix).
						// Both pipelines may report the same multi-arch tags, so dedupe.
						for _, tag := range img.MultiArchTags {
							report.Images = appendUniqueImage(report.Images, schema.ImageArtifact{
								Platform: MultiArch,
								URL:      img.Repo + ":" + tag,
							})
						}
					}
				}
			case "pushed-binaries":
				var bin binariesResultYAML
				if err := yaml.Unmarshal([]byte(r.Value.StringVal), &bin); err == nil && bin.Oci != nil {
					report.Binaries = append(report.Binaries, schema.OciArtifact{
						Repo:     bin.Oci.Repo,
						Tag:      bin.Oci.Tag,
						Files:    bin.Files,
						Platform: parsePlatformFromParams(pr.Spec.Params),
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

// appendUniqueImage appends img to images unless an entry with the same
// platform and URL already exists (e.g. both pipelines reporting the same
// multi-arch tags).
func appendUniqueImage(images []schema.ImageArtifact, img schema.ImageArtifact) []schema.ImageArtifact {
	for _, existing := range images {
		if existing.Platform == img.Platform && existing.URL == img.URL {
			return images
		}
	}
	return append(images, img)
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

// Serverless requests finish only after every requested platform has a run.
func determineBuildStatus(build *ent.DevBuild, runs []tknv1.PipelineRun) string {
	status := determineStatus(runs)
	if !isServerlessProduct(build.Product) || status != "SUCCESS" {
		return status
	}
	seen := map[string]bool{}
	for _, run := range runs {
		seen[parsePlatformFromParams(run.Spec.Params)] = true
	}
	for _, platform := range parsePlatforms(build.Platform) {
		if !seen[platform] {
			return "PROCESSING"
		}
	}
	for _, run := range runs {
		_, images := extractArtifactsFromResults(run.Status.Results, run.Spec.Params)
		if len(images) == 0 {
			return "FAILURE"
		}
		for _, image := range images {
			if at := strings.LastIndex(image.URL, ":"); at <= 0 || at <= strings.LastIndex(image.URL, "/") || at == len(image.URL)-1 {
				return "FAILURE"
			}
		}
	}
	return status
}
