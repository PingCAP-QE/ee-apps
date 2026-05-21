package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/google/go-github/v68/github"
)

const (
	imageTagWorkflowRunLookupTimeout = 30 * time.Second
	imageTagWorkflowRunLookupTick    = 2 * time.Second
	imageTagHTTPTimeout              = 30 * time.Second
)

var errImageTagWorkflowRunNotFound = errors.New("dispatched workflow run not found yet")

type imageTagTriggerParams struct {
	Registry string
	Tag      string
}

func runCommandImageTagTrigger(ctx context.Context, args []string) (string, error) {
	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return imageTagHelpText, NewInformationError("Requested command usage")
	}

	params, err := parseCommandImageTagTrigger(args)
	if err != nil {
		return "", err
	}

	cfg, token, err := loadImageTagWorkflowConfig(ctx)
	if err != nil {
		return "", err
	}

	gc, _ := newImageTagGitHubClient(token)
	return triggerImageTagWorkflow(ctx, params, gc, cfg)
}

func parseCommandImageTagTrigger(args []string) (*imageTagTriggerParams, error) {
	if len(args) != 2 {
		return nil, errors.New(imageTagHelpText)
	}

	registry := strings.TrimSpace(args[0])
	tag := strings.TrimSpace(args[1])
	if registry == "" || tag == "" {
		return nil, errors.New(imageTagHelpText)
	}

	return &imageTagTriggerParams{
		Registry: registry,
		Tag:      tag,
	}, nil
}

func triggerImageTagWorkflow(ctx context.Context, params *imageTagTriggerParams, gc *github.Client, cfg imageTagWorkflowConfig) (string, error) {
	ref, err := resolveImageTagWorkflowRef(ctx, gc, cfg)
	if err != nil {
		return "", err
	}

	maxRunID, err := maxImageTagWorkflowRunID(ctx, gc, cfg)
	if err != nil {
		return "", err
	}

	dispatchedAt := time.Now().UTC()
	inputs := map[string]interface{}{
		"registry_url": params.Registry,
		"image_tag":    params.Tag,
	}
	if credentialRef := resolveImageTagCredentialRef(params.Registry, cfg.CredentialRefs); credentialRef != "" {
		inputs["credential_ref"] = credentialRef
	}

	_, err = gc.Actions.CreateWorkflowDispatchEventByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow, github.CreateWorkflowDispatchEventRequest{
		Ref:    ref,
		Inputs: inputs,
	})
	if err != nil {
		return "", fmt.Errorf("trigger image-tag workflow failed: %w", err)
	}

	run, err := waitForImageTagWorkflowRun(ctx, gc, cfg, ref, params.Registry, params.Tag, maxRunID, dispatchedAt)
	if err != nil {
		return "", err
	}

	runURL := buildImageTagRunURL(cfg, run.GetID(), run.GetHTMLURL())
	return fmt.Sprintf("workflow run id is %d\npoll command: /image-tag poll %d\nrun: %s", run.GetID(), run.GetID(), runURL), nil
}

func resolveImageTagWorkflowRef(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig) (string, error) {
	if cfg.Ref != "" {
		return cfg.Ref, nil
	}

	repo, _, err := gc.Repositories.Get(ctx, cfg.Owner, cfg.Repo)
	if err != nil {
		return "", fmt.Errorf("resolve default branch for %s/%s failed: %w", cfg.Owner, cfg.Repo, err)
	}

	if repo.GetDefaultBranch() == "" {
		return "", fmt.Errorf("repository %s/%s does not report a default branch", cfg.Owner, cfg.Repo)
	}

	return repo.GetDefaultBranch(), nil
}

func maxImageTagWorkflowRunID(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig) (int64, error) {
	runs, _, err := gc.Actions.ListWorkflowRunsByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow, &github.ListWorkflowRunsOptions{
		ListOptions: github.ListOptions{PerPage: 1},
	})
	if err != nil {
		return 0, fmt.Errorf("list existing workflow runs failed: %w", err)
	}

	if runs == nil || len(runs.WorkflowRuns) == 0 || runs.WorkflowRuns[0] == nil {
		return 0, nil
	}

	return runs.WorkflowRuns[0].GetID(), nil
}

func waitForImageTagWorkflowRun(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig, ref, registry, tag string, minRunID int64, dispatchedAt time.Time) (*github.WorkflowRun, error) {
	waitCtx, cancel := context.WithTimeout(ctx, imageTagWorkflowRunLookupTimeout)
	defer cancel()

	ticker := time.NewTicker(imageTagWorkflowRunLookupTick)
	defer ticker.Stop()

	for {
		run, err := findDispatchedImageTagWorkflowRun(waitCtx, gc, cfg, ref, registry, tag, minRunID, dispatchedAt)
		if err == nil {
			return run, nil
		}
		if !errors.Is(err, errImageTagWorkflowRunNotFound) {
			return nil, err
		}

		select {
		case <-waitCtx.Done():
			return nil, fmt.Errorf("workflow dispatched but run id was not visible yet; check %s", buildWorkflowPageURL(cfg))
		case <-ticker.C:
		}
	}
}

func findDispatchedImageTagWorkflowRun(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig, ref, registry, tag string, minRunID int64, dispatchedAt time.Time) (*github.WorkflowRun, error) {
	runs, _, err := gc.Actions.ListWorkflowRunsByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow, &github.ListWorkflowRunsOptions{
		Event:       "workflow_dispatch",
		ListOptions: github.ListOptions{PerPage: 20},
	})
	if err != nil {
		return nil, fmt.Errorf("list dispatched workflow runs failed: %w", err)
	}
	if runs == nil {
		return nil, errImageTagWorkflowRunNotFound
	}

	expectedTitle := fmt.Sprintf("query-image-tag %s:%s", registry, tag)
	run := selectImageTagWorkflowRun(runs.WorkflowRuns, ref, expectedTitle, minRunID, dispatchedAt)
	if run == nil {
		return nil, errImageTagWorkflowRunNotFound
	}

	return run, nil
}

func selectImageTagWorkflowRun(runs []*github.WorkflowRun, ref, expectedTitle string, minRunID int64, dispatchedAt time.Time) *github.WorkflowRun {
	var fallback *github.WorkflowRun

	for _, run := range runs {
		if run == nil || run.GetID() <= minRunID {
			continue
		}
		if run.GetEvent() != "workflow_dispatch" {
			continue
		}
		if !imageTagWorkflowRunMatchesRef(run, ref) {
			continue
		}
		if run.CreatedAt != nil && run.CreatedAt.Time.Before(dispatchedAt.Add(-1*time.Minute)) {
			continue
		}

		if run.GetDisplayTitle() == expectedTitle {
			return run
		}

		if fallback == nil || run.GetID() > fallback.GetID() {
			fallback = run
		}
	}

	return fallback
}

func imageTagWorkflowRunMatchesRef(run *github.WorkflowRun, ref string) bool {
	if ref == "" {
		return true
	}

	if run.GetHeadSHA() == ref || run.GetHeadBranch() == ref {
		return true
	}

	if strings.HasPrefix(ref, "refs/heads/") && run.GetHeadBranch() == strings.TrimPrefix(ref, "refs/heads/") {
		return true
	}

	if strings.HasPrefix(ref, "refs/tags/") && run.GetHeadBranch() == strings.TrimPrefix(ref, "refs/tags/") {
		return true
	}

	return false
}

func newImageTagGitHubClient(token string) (*github.Client, *http.Client) {
	httpClient := &http.Client{Timeout: imageTagHTTPTimeout}
	return github.NewClient(httpClient).WithAuthToken(token), httpClient
}

func resolveImageTagCredentialRef(registry string, credentialRefs map[string]string) string {
	registry = normalizeImageTagRegistryPrefix(registry)
	if registry == "" {
		return ""
	}

	var matchedPrefix string
	var matchedCredentialRef string
	for prefix, credentialRef := range credentialRefs {
		normalizedPrefix := normalizeImageTagRegistryPrefix(prefix)
		if normalizedPrefix == "" || credentialRef == "" {
			continue
		}
		if !imageTagRegistryPrefixMatches(registry, normalizedPrefix) {
			continue
		}
		if len(normalizedPrefix) > len(matchedPrefix) {
			matchedPrefix = normalizedPrefix
			matchedCredentialRef = credentialRef
		}
	}

	return matchedCredentialRef
}

func normalizeImageTagRegistryPrefix(registry string) string {
	registry = strings.TrimSpace(strings.ToLower(registry))
	registry = strings.TrimPrefix(registry, "https://")
	registry = strings.TrimPrefix(registry, "http://")
	return strings.Trim(registry, "/")
}

func imageTagRegistryPrefixMatches(registry, prefix string) bool {
	return registry == prefix || strings.HasPrefix(registry, prefix+"/")
}

func buildWorkflowPageURL(cfg imageTagWorkflowConfig) string {
	return fmt.Sprintf("https://github.com/%s/%s/actions/workflows/%s", cfg.Owner, cfg.Repo, cfg.Workflow)
}

func buildImageTagRunURL(cfg imageTagWorkflowConfig, runID int64, htmlURL string) string {
	if htmlURL != "" {
		return htmlURL
	}

	return fmt.Sprintf("https://github.com/%s/%s/actions/runs/%d", cfg.Owner, cfg.Repo, runID)
}
