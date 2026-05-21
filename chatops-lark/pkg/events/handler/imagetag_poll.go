package handler

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Masterminds/sprig/v3"
	"github.com/google/go-github/v68/github"
	"gopkg.in/yaml.v3"

	_ "embed"
)

const registryImageArtifactName = "result.json"

//go:embed imagetag_poll.md.tmpl
var registryImageResponseTmpl string

type registryImageQueryStatus string

const (
	registryImageQueryStatusFound      registryImageQueryStatus = "FOUND"
	registryImageQueryStatusNotFound   registryImageQueryStatus = "NOT_FOUND"
	registryImageQueryStatusAuthFailed registryImageQueryStatus = "AUTH_FAILED"
	registryImageQueryStatusRunning    registryImageQueryStatus = "RUNNING"
	registryImageQueryStatusTimeout    registryImageQueryStatus = "TIMEOUT"
	registryImageQueryStatusFailed     registryImageQueryStatus = "FAILED"
)

type registryImageArtifactResult struct {
	ImageRef  string         `json:"image_ref"`
	CreatedAt string         `json:"created_at"`
	Digest    string         `json:"digest"`
	MultiArch bool           `json:"multi_arch"`
	Platforms []string       `json:"platforms"`
	Labels    map[string]any `json:"labels"`
}

type registryImageQueryOutcome struct {
	Status    registryImageQueryStatus
	Summary   string
	ImageRef  string
	CreatedAt string
	Digest    string
	MultiArch bool
	Platforms []string
	Labels    map[string]any
	RunURL    string
}

type registryImageFailureStep struct {
	JobID      int64
	JobName    string
	StepName   string
	Conclusion string
}

func (outcome *registryImageQueryOutcome) responseStatus() string {
	switch outcome.Status {
	case registryImageQueryStatusFound:
		return StatusSuccess
	case registryImageQueryStatusNotFound, registryImageQueryStatusRunning, registryImageQueryStatusTimeout:
		return StatusInfo
	default:
		return StatusFailure
	}
}

func newRegistryImageRunningOutcome(imageRef, runURL, summary string, launched bool) *registryImageQueryOutcome {
	if launched {
		summary = summary + " I will reply in this thread with the final result."
	} else {
		summary = summary + " Check the GitHub Actions workflow page for the final result."
	}

	return &registryImageQueryOutcome{
		Status:   registryImageQueryStatusRunning,
		Summary:  summary,
		ImageRef: imageRef,
		RunURL:   runURL,
	}
}

func newRegistryImageTimeoutOutcome(imageRef, runURL, summary string) *registryImageQueryOutcome {
	return &registryImageQueryOutcome{
		Status:   registryImageQueryStatusTimeout,
		Summary:  summary,
		ImageRef: imageRef,
		RunURL:   runURL,
	}
}

func newRegistryImageFailedOutcome(imageRef, runURL, summary string) *registryImageQueryOutcome {
	return &registryImageQueryOutcome{
		Status:   registryImageQueryStatusFailed,
		Summary:  summary,
		ImageRef: imageRef,
		RunURL:   runURL,
	}
}

func waitForRegistryImageWorkflowCompletion(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, runID int64) (*github.WorkflowRun, error) {
	ticker := time.NewTicker(registryImageWorkflowStatusPollTick)
	defer ticker.Stop()

	for {
		run, _, err := gc.Actions.GetWorkflowRunByID(ctx, cfg.Owner, cfg.Repo, runID)
		if err != nil {
			return nil, fmt.Errorf("get registry image workflow run failed: %w", err)
		}
		if err := validateRegistryImageWorkflowRun(ctx, gc, cfg, run); err != nil {
			return nil, err
		}
		if run.GetStatus() == "completed" {
			return run, nil
		}

		select {
		case <-ctx.Done():
			return run, ctx.Err()
		case <-ticker.C:
		}
	}
}

func buildRegistryImageQueryOutcome(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, run *github.WorkflowRun, httpClient *http.Client) (*registryImageQueryOutcome, error) {
	if err := validateRegistryImageWorkflowRun(ctx, gc, cfg, run); err != nil {
		return nil, err
	}

	runURL := buildRegistryImageRunURL(cfg, run.GetID(), run.GetHTMLURL())
	imageRef := registryImageRefFromRun(run)
	if run.GetConclusion() == "success" {
		result, err := downloadRegistryImageResult(ctx, gc, cfg, run.GetID(), httpClient)
		if err != nil {
			return nil, err
		}

		return &registryImageQueryOutcome{
			Status:    registryImageQueryStatusFound,
			Summary:   "Tag exists in the target registry.",
			ImageRef:  result.ImageRef,
			CreatedAt: result.CreatedAt,
			Digest:    result.Digest,
			MultiArch: result.MultiArch,
			Platforms: result.Platforms,
			Labels:    result.Labels,
			RunURL:    runURL,
		}, nil
	}

	return buildRegistryImageFailureOutcome(ctx, gc, cfg, run, imageRef, runURL, httpClient), nil
}

func buildRegistryImageFailureOutcome(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, run *github.WorkflowRun, imageRef, runURL string, httpClient *http.Client) *registryImageQueryOutcome {
	if run == nil {
		return newRegistryImageFailedOutcome(imageRef, runURL, "GitHub Actions returned no workflow payload.")
	}

	if run.GetConclusion() == "timed_out" {
		return newRegistryImageTimeoutOutcome(imageRef, runURL, "GitHub Actions timed out before the registry query finished.")
	}

	failureStep, logs := inspectRegistryImageFailure(ctx, gc, cfg, run, httpClient)
	if failureStep != nil {
		if failureStep.Conclusion == "timed_out" {
			return newRegistryImageTimeoutOutcome(imageRef, runURL, fmt.Sprintf("The %q step timed out in GitHub Actions.", failureStep.StepName))
		}

		switch failureStep.StepName {
		case "Authenticate registry":
			return &registryImageQueryOutcome{
				Status:   registryImageQueryStatusAuthFailed,
				Summary:  "Failed to authenticate to the target registry.",
				ImageRef: imageRef,
				RunURL:   runURL,
			}
		case "Inspect image and write result artifact":
			switch {
			case registryImageLogLooksNotFound(logs):
				return &registryImageQueryOutcome{
					Status:   registryImageQueryStatusNotFound,
					Summary:  "The tag does not exist in the target registry.",
					ImageRef: imageRef,
					RunURL:   runURL,
				}
			case registryImageLogLooksAuthFailure(logs):
				return &registryImageQueryOutcome{
					Status:   registryImageQueryStatusAuthFailed,
					Summary:  "The registry rejected authentication for this image query.",
					ImageRef: imageRef,
					RunURL:   runURL,
				}
			}
		}
	}

	switch run.GetConclusion() {
	case "cancelled":
		return newRegistryImageFailedOutcome(imageRef, runURL, "The GitHub Actions workflow was cancelled before the registry query finished.")
	default:
		return newRegistryImageFailedOutcome(imageRef, runURL, fmt.Sprintf("GitHub Actions finished with conclusion %q.", run.GetConclusion()))
	}
}

func inspectRegistryImageFailure(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, run *github.WorkflowRun, httpClient *http.Client) (*registryImageFailureStep, string) {
	jobs, _, err := gc.Actions.ListWorkflowJobs(ctx, cfg.Owner, cfg.Repo, run.GetID(), &github.ListWorkflowJobsOptions{
		Filter:      "latest",
		ListOptions: github.ListOptions{PerPage: 100},
	})
	if err != nil || jobs == nil {
		return nil, ""
	}

	failureStep := findRegistryImageFailureStep(jobs.Jobs)
	if failureStep == nil {
		return nil, ""
	}

	logs, err := downloadRegistryImageJobLogs(ctx, gc, cfg, failureStep.JobID, httpClient)
	if err != nil {
		return failureStep, ""
	}

	return failureStep, logs
}

func findRegistryImageFailureStep(jobs []*github.WorkflowJob) *registryImageFailureStep {
	for _, job := range jobs {
		if job == nil {
			continue
		}

		for _, step := range job.Steps {
			stepName := registryImageTaskStepName(step)
			conclusion := registryImageTaskStepConclusion(step)
			if conclusion == "" || conclusion == "success" || conclusion == "skipped" {
				continue
			}

			return &registryImageFailureStep{
				JobID:      job.GetID(),
				JobName:    job.GetName(),
				StepName:   stepName,
				Conclusion: conclusion,
			}
		}

		if conclusion := job.GetConclusion(); conclusion != "" && conclusion != "success" && conclusion != "skipped" {
			return &registryImageFailureStep{
				JobID:      job.GetID(),
				JobName:    job.GetName(),
				StepName:   job.GetName(),
				Conclusion: conclusion,
			}
		}
	}

	return nil
}

func registryImageTaskStepName(step *github.TaskStep) string {
	if step == nil || step.Name == nil {
		return ""
	}

	return strings.TrimSpace(*step.Name)
}

func registryImageTaskStepConclusion(step *github.TaskStep) string {
	if step == nil || step.Conclusion == nil {
		return ""
	}

	return strings.TrimSpace(*step.Conclusion)
}

func downloadRegistryImageJobLogs(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, jobID int64, httpClient *http.Client) (string, error) {
	if jobID == 0 {
		return "", nil
	}

	logURL, _, err := gc.Actions.GetWorkflowJobLogs(ctx, cfg.Owner, cfg.Repo, jobID, 1)
	if err != nil {
		return "", fmt.Errorf("download workflow job logs redirect failed: %w", err)
	}
	if logURL == nil {
		return "", fmt.Errorf("download workflow job logs redirect returned no URL")
	}

	resp, err := httpClient.Get(logURL.String())
	if err != nil {
		return "", fmt.Errorf("download workflow job logs failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download workflow job logs returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read workflow job logs failed: %w", err)
	}

	return strings.ToLower(string(body)), nil
}

func registryImageLogLooksNotFound(logs string) bool {
	return strings.Contains(logs, "not found")
}

func registryImageLogLooksAuthFailure(logs string) bool {
	for _, marker := range []string{"unauthorized", "authentication required", "no basic auth credentials", "denied", "forbidden"} {
		if strings.Contains(logs, marker) {
			return true
		}
	}

	return false
}

func downloadRegistryImageResult(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, runID int64, httpClient *http.Client) (*registryImageArtifactResult, error) {
	artifact, err := getRegistryImageArtifact(ctx, gc, cfg, runID)
	if err != nil {
		return nil, err
	}

	downloadURL, _, err := gc.Actions.DownloadArtifact(ctx, cfg.Owner, cfg.Repo, artifact.GetID(), 0)
	if err != nil {
		return nil, fmt.Errorf("download result artifact redirect failed: %w", err)
	}
	if downloadURL == nil {
		return nil, fmt.Errorf("download result artifact redirect returned no URL")
	}

	archiveBytes, err := fetchRegistryImageArtifactArchive(ctx, httpClient, downloadURL.String())
	if err != nil {
		return nil, err
	}

	resultBytes, err := extractResultJSONFromArtifact(archiveBytes, registryImageArtifactName)
	if err != nil {
		return nil, err
	}

	var result registryImageArtifactResult
	if err := json.Unmarshal(resultBytes, &result); err != nil {
		return nil, fmt.Errorf("decode result.json failed: %w", err)
	}

	if result.Labels == nil {
		result.Labels = map[string]any{}
	}

	return &result, nil
}

func validateRegistryImageWorkflowRun(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, run *github.WorkflowRun) error {
	if run == nil {
		return fmt.Errorf("workflow run returned no payload")
	}

	expectedPath := registryImageWorkflowPath(cfg.Workflow)
	if workflowPathMatches(run.GetPath(), expectedPath) {
		return nil
	}

	workflow, _, err := gc.Actions.GetWorkflowByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow)
	if err != nil {
		return fmt.Errorf("load workflow metadata for %q failed: %w", cfg.Workflow, err)
	}
	if workflow == nil {
		return fmt.Errorf("workflow metadata for %q returned no payload", cfg.Workflow)
	}
	if registryImageWorkflowRunMatches(run, workflow) {
		return nil
	}

	return fmt.Errorf(
		"workflow run %d does not belong to %q (got %q)",
		run.GetID(),
		describeRegistryImageWorkflow(workflow, expectedPath),
		describeRegistryImageWorkflowRun(run),
	)
}

func getRegistryImageArtifact(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, runID int64) (*github.Artifact, error) {
	artifacts, _, err := gc.Actions.ListWorkflowRunArtifacts(ctx, cfg.Owner, cfg.Repo, runID, &github.ListOptions{PerPage: 100})
	if err != nil {
		return nil, fmt.Errorf("list workflow run artifacts failed: %w", err)
	}
	if artifacts == nil {
		return nil, fmt.Errorf("workflow run %d returned no artifacts payload", runID)
	}

	for _, artifact := range artifacts.Artifacts {
		if artifact == nil {
			continue
		}
		if artifact.GetName() != registryImageArtifactName || artifact.GetExpired() {
			continue
		}
		return artifact, nil
	}

	return nil, fmt.Errorf("workflow run %d did not produce a %s artifact", runID, registryImageArtifactName)
}

func registryImageWorkflowRunMatches(run *github.WorkflowRun, workflow *github.Workflow) bool {
	if run == nil || workflow == nil {
		return false
	}
	if workflow.GetID() != 0 && run.GetWorkflowID() == workflow.GetID() {
		return true
	}
	if workflow.GetURL() != "" && strings.TrimSpace(run.GetWorkflowURL()) == strings.TrimSpace(workflow.GetURL()) {
		return true
	}
	return workflowPathMatches(run.GetPath(), workflow.GetPath())
}

func workflowPathMatches(actualPath, expectedPath string) bool {
	actual := normalizeWorkflowPath(actualPath)
	expected := normalizeWorkflowPath(expectedPath)
	if actual == "" || expected == "" {
		return false
	}
	return actual == expected
}

func registryImageWorkflowPath(workflow string) string {
	path := normalizeWorkflowPath(workflow)
	if path == "" {
		return ""
	}
	if strings.Contains(path, "/") {
		return path
	}
	return ".github/workflows/" + path
}

func normalizeWorkflowPath(path string) string {
	path = strings.TrimSpace(path)
	path = strings.TrimPrefix(path, "/")
	if idx := strings.Index(path, "@"); idx >= 0 {
		path = path[:idx]
	}
	return path
}

func describeRegistryImageWorkflowRun(run *github.WorkflowRun) string {
	if run == nil {
		return "unknown workflow"
	}
	if path := normalizeWorkflowPath(run.GetPath()); path != "" {
		return path
	}
	if url := strings.TrimSpace(run.GetWorkflowURL()); url != "" {
		return url
	}
	if workflowID := run.GetWorkflowID(); workflowID != 0 {
		return fmt.Sprintf("workflow id %d", workflowID)
	}
	return "unknown workflow"
}

func describeRegistryImageWorkflow(workflow *github.Workflow, fallbackPath string) string {
	if workflow != nil {
		if path := normalizeWorkflowPath(workflow.GetPath()); path != "" {
			return path
		}
		if url := strings.TrimSpace(workflow.GetURL()); url != "" {
			return url
		}
		if workflowID := workflow.GetID(); workflowID != 0 {
			return fmt.Sprintf("workflow id %d", workflowID)
		}
	}
	if path := normalizeWorkflowPath(fallbackPath); path != "" {
		return path
	}
	return strings.TrimSpace(fallbackPath)
}

func fetchRegistryImageArtifactArchive(ctx context.Context, httpClient *http.Client, archiveURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, archiveURL, nil)
	if err != nil {
		return nil, fmt.Errorf("build artifact download request failed: %w", err)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("download artifact archive failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download artifact archive failed: unexpected status %s", resp.Status)
	}

	archiveBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read artifact archive failed: %w", err)
	}

	return archiveBytes, nil
}

func extractResultJSONFromArtifact(archiveBytes []byte, filename string) ([]byte, error) {
	reader, err := zip.NewReader(bytes.NewReader(archiveBytes), int64(len(archiveBytes)))
	if err != nil {
		return nil, fmt.Errorf("open artifact archive failed: %w", err)
	}

	for _, file := range reader.File {
		if file == nil {
			continue
		}
		if file.Name != filename && !strings.HasSuffix(file.Name, "/"+filename) {
			continue
		}

		rc, err := file.Open()
		if err != nil {
			return nil, fmt.Errorf("open %s from artifact failed: %w", filename, err)
		}

		body, readErr := io.ReadAll(rc)
		closeErr := rc.Close()
		if readErr != nil {
			return nil, fmt.Errorf("read %s from artifact failed: %w", filename, readErr)
		}
		if closeErr != nil {
			return nil, fmt.Errorf("close %s from artifact failed: %w", filename, closeErr)
		}

		return body, nil
	}

	return nil, fmt.Errorf("artifact archive does not contain %s", filename)
}

func renderRegistryImageQueryResponse(outcome *registryImageQueryOutcome) (string, error) {
	tmpl := template.Must(template.New("markdown").
		Funcs(sprig.FuncMap()).
		Funcs(template.FuncMap{
			"toYaml": func(v any) string {
				yamlBytes, err := yaml.Marshal(v)
				if err != nil {
					return fmt.Sprintf("failed to marshal to YAML: %v", err)
				}
				return strings.TrimSuffix(string(yamlBytes), "\n")
			},
		}).
		Parse(registryImageResponseTmpl))

	data := map[string]any{
		"status":     string(outcome.Status),
		"summary":    outcome.Summary,
		"image_ref":  outcome.ImageRef,
		"created_at": outcome.CreatedAt,
		"digest":     outcome.Digest,
		"multi_arch": outcome.MultiArch,
		"platforms":  outcome.Platforms,
		"labels":     outcome.Labels,
		"run_url":    outcome.RunURL,
		"is_found":   outcome.Status == registryImageQueryStatusFound,
	}

	var sb strings.Builder
	if err := tmpl.Execute(&sb, data); err != nil {
		return "", fmt.Errorf("render registry image reply failed: %w", err)
	}

	return sb.String(), nil
}

func mustRenderRegistryImageQueryResponse(outcome *registryImageQueryOutcome) string {
	message, err := renderRegistryImageQueryResponse(outcome)
	if err == nil {
		return message
	}

	lines := []string{
		fmt.Sprintf("Registry image query: %s", outcome.Status),
		outcome.Summary,
	}
	if outcome.ImageRef != "" {
		lines = append(lines, fmt.Sprintf("Image: %s", outcome.ImageRef))
	}
	if outcome.RunURL != "" {
		lines = append(lines, fmt.Sprintf("Run: %s", outcome.RunURL))
	}
	lines = append(lines, fmt.Sprintf("Render error: %v", err))

	return strings.Join(lines, "\n")
}

func registryImageRefFromRun(run *github.WorkflowRun) string {
	if run == nil {
		return ""
	}

	title := strings.TrimSpace(run.GetDisplayTitle())
	const prefix = "query-image-tag "
	if strings.HasPrefix(title, prefix) {
		return strings.TrimSpace(strings.TrimPrefix(title, prefix))
	}

	return ""
}
