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

var (
	registryImageInlineWaitTimeout      = 90 * time.Second
	registryImageAsyncWaitTimeout       = 15 * time.Minute
	registryImageWorkflowRunLookupTick  = 2 * time.Second
	registryImageWorkflowStatusPollTick = 5 * time.Second
	registryImageHTTPTimeout            = 30 * time.Second
)

var errRegistryImageWorkflowRunNotFound = errors.New("dispatched workflow run not found yet")

type registryImageQueryParams struct {
	Repository string
	Tag        string
	ImageRef   string
}

type registryImageAsyncState struct {
	Params       *registryImageQueryParams
	Ref          string
	MinRunID     int64
	DispatchedAt time.Time
	RunID        int64
}

func runCommandRegistryImageQuery(ctx context.Context, args []string) (string, error) {
	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return registryImageHelpText, NewInformationError("Requested command usage")
	}

	params, err := parseCommandRegistryImageQuery(args)
	if err != nil {
		return "", err
	}

	cfg, token, err := loadRegistryImageWorkflowConfig(ctx)
	if err != nil {
		return "", err
	}

	gc, httpClient := newRegistryImageGitHubClient(token)
	return queryRegistryImage(ctx, params, gc, cfg, httpClient)
}

func parseCommandRegistryImageQuery(args []string) (*registryImageQueryParams, error) {
	if len(args) == 0 {
		return nil, errors.New(registryImageHelpText)
	}

	var repository string
	var tag string

	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--repo":
			if i+1 >= len(args) || args[i+1] == "" {
				return nil, errors.New(registryImageHelpText)
			}
			if repository != "" {
				return nil, errors.New(registryImageHelpText)
			}
			repository = args[i+1]
			i++
		case "--tag":
			if i+1 >= len(args) || args[i+1] == "" {
				return nil, errors.New(registryImageHelpText)
			}
			if tag != "" {
				return nil, errors.New(registryImageHelpText)
			}
			tag = args[i+1]
			i++
		default:
			return nil, errors.New(registryImageHelpText)
		}
	}

	if repository == "" || tag == "" {
		return nil, errors.New(registryImageHelpText)
	}

	repository = strings.TrimSpace(repository)
	tag = strings.TrimSpace(tag)
	if repository == "" || tag == "" || strings.Contains(repository, "@") {
		return nil, errors.New(registryImageHelpText)
	}

	return &registryImageQueryParams{
		Repository: repository,
		Tag:        tag,
		ImageRef:   fmt.Sprintf("%s:%s", repository, tag),
	}, nil
}

func queryRegistryImage(ctx context.Context, params *registryImageQueryParams, gc *github.Client, cfg registryImageWorkflowConfig, httpClient *http.Client) (string, error) {
	ref, err := resolveRegistryImageWorkflowRef(ctx, gc, cfg)
	if err != nil {
		return "", err
	}

	maxRunID, err := maxRegistryImageWorkflowRunID(ctx, gc, cfg)
	if err != nil {
		return "", err
	}

	dispatchedAt := time.Now().UTC()
	inputs := map[string]any{
		"registry_url": params.Repository,
		"image_tag":    params.Tag,
	}

	_, err = gc.Actions.CreateWorkflowDispatchEventByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow, github.CreateWorkflowDispatchEventRequest{
		Ref:    ref,
		Inputs: inputs,
	})
	if err != nil {
		return "", fmt.Errorf("trigger registry image workflow failed: %w", err)
	}

	inlineCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), registryImageInlineWaitTimeout)
	defer cancel()

	run, err := waitForRegistryImageWorkflowRun(inlineCtx, gc, cfg, ref, params.Repository, params.Tag, maxRunID, dispatchedAt)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			launched := scheduleAsyncRegistryImageQueryReply(ctx, gc, cfg, httpClient, registryImageAsyncState{
				Params:       params,
				Ref:          ref,
				MinRunID:     maxRunID,
				DispatchedAt: dispatchedAt,
			})
			outcome := newRegistryImageRunningOutcome(
				params.ImageRef,
				buildWorkflowPageURL(cfg),
				fmt.Sprintf("GitHub accepted the query, but the workflow run is not visible yet after %s.", registryImageInlineWaitTimeout),
				launched,
			)
			setCommandResponseStatus(ctx, outcome.responseStatus())
			return renderRegistryImageQueryResponse(outcome)
		}

		return "", err
	}

	completedRun, err := waitForRegistryImageWorkflowCompletion(inlineCtx, gc, cfg, run.GetID())
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			launched := scheduleAsyncRegistryImageQueryReply(ctx, gc, cfg, httpClient, registryImageAsyncState{
				Params:       params,
				Ref:          ref,
				MinRunID:     maxRunID,
				DispatchedAt: dispatchedAt,
				RunID:        run.GetID(),
			})
			outcome := newRegistryImageRunningOutcome(
				params.ImageRef,
				buildRegistryImageRunURL(cfg, run.GetID(), run.GetHTMLURL()),
				fmt.Sprintf("The workflow is still running after %s.", registryImageInlineWaitTimeout),
				launched,
			)
			setCommandResponseStatus(ctx, outcome.responseStatus())
			return renderRegistryImageQueryResponse(outcome)
		}

		return "", err
	}

	outcome, err := buildRegistryImageQueryOutcome(inlineCtx, gc, cfg, completedRun, httpClient)
	if err != nil {
		return "", err
	}

	setCommandResponseStatus(ctx, outcome.responseStatus())
	return renderRegistryImageQueryResponse(outcome)
}

func scheduleAsyncRegistryImageQueryReply(parentCtx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, httpClient *http.Client, state registryImageAsyncState) bool {
	if !hasCommandReply(parentCtx) {
		return false
	}

	go func() {
		asyncCtx, cancel := context.WithTimeout(context.Background(), registryImageAsyncWaitTimeout)
		defer cancel()

		run, err := awaitRegistryImageQueryCompletion(asyncCtx, gc, cfg, state)
		if err != nil {
			outcome := newRegistryImageTimeoutOutcome(
				state.Params.ImageRef,
				"",
				fmt.Sprintf("GitHub Actions did not finish within %s.", registryImageAsyncWaitTimeout),
			)
			if run != nil {
				outcome.RunURL = buildRegistryImageRunURL(cfg, run.GetID(), run.GetHTMLURL())
			}
			if !errors.Is(err, context.DeadlineExceeded) {
				outcome = newRegistryImageFailedOutcome(
					state.Params.ImageRef,
					outcome.RunURL,
					fmt.Sprintf("Unexpected error while waiting for the GitHub Actions result: %v", err),
				)
			}
			_ = sendCommandReply(parentCtx, outcome.responseStatus(), mustRenderRegistryImageQueryResponse(outcome))
			return
		}

		outcome, err := buildRegistryImageQueryOutcome(asyncCtx, gc, cfg, run, httpClient)
		if err != nil {
			fallback := newRegistryImageFailedOutcome(
				state.Params.ImageRef,
				buildRegistryImageRunURL(cfg, run.GetID(), run.GetHTMLURL()),
				fmt.Sprintf("Unexpected error while loading the GitHub Actions result: %v", err),
			)
			_ = sendCommandReply(parentCtx, fallback.responseStatus(), mustRenderRegistryImageQueryResponse(fallback))
			return
		}

		_ = sendCommandReply(parentCtx, outcome.responseStatus(), mustRenderRegistryImageQueryResponse(outcome))
	}()

	return true
}

func awaitRegistryImageQueryCompletion(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, state registryImageAsyncState) (*github.WorkflowRun, error) {
	var (
		run *github.WorkflowRun
		err error
	)

	if state.RunID != 0 {
		run, err = waitForRegistryImageWorkflowCompletion(ctx, gc, cfg, state.RunID)
		if err != nil {
			return run, err
		}
		return run, nil
	}

	run, err = waitForRegistryImageWorkflowRun(ctx, gc, cfg, state.Ref, state.Params.Repository, state.Params.Tag, state.MinRunID, state.DispatchedAt)
	if err != nil {
		return nil, err
	}

	run, err = waitForRegistryImageWorkflowCompletion(ctx, gc, cfg, run.GetID())
	if err != nil {
		return run, err
	}

	return run, nil
}

func resolveRegistryImageWorkflowRef(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig) (string, error) {
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

func maxRegistryImageWorkflowRunID(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig) (int64, error) {
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

func waitForRegistryImageWorkflowRun(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, ref, repository, tag string, minRunID int64, dispatchedAt time.Time) (*github.WorkflowRun, error) {
	ticker := time.NewTicker(registryImageWorkflowRunLookupTick)
	defer ticker.Stop()

	for {
		run, err := findDispatchedRegistryImageWorkflowRun(ctx, gc, cfg, ref, repository, tag, minRunID, dispatchedAt)
		if err == nil {
			return run, nil
		}
		if !errors.Is(err, errRegistryImageWorkflowRunNotFound) {
			return nil, err
		}

		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.C:
		}
	}
}

func findDispatchedRegistryImageWorkflowRun(ctx context.Context, gc *github.Client, cfg registryImageWorkflowConfig, ref, repository, tag string, minRunID int64, dispatchedAt time.Time) (*github.WorkflowRun, error) {
	runs, _, err := gc.Actions.ListWorkflowRunsByFileName(ctx, cfg.Owner, cfg.Repo, cfg.Workflow, &github.ListWorkflowRunsOptions{
		Event:       "workflow_dispatch",
		ListOptions: github.ListOptions{PerPage: 20},
	})
	if err != nil {
		return nil, fmt.Errorf("list dispatched registry image workflow runs failed: %w", err)
	}
	if runs == nil {
		return nil, errRegistryImageWorkflowRunNotFound
	}

	expectedTitle := fmt.Sprintf("query-image-tag %s:%s", repository, tag)
	run := selectRegistryImageWorkflowRun(runs.WorkflowRuns, ref, expectedTitle, minRunID, dispatchedAt)
	if run == nil {
		return nil, errRegistryImageWorkflowRunNotFound
	}

	return run, nil
}

func selectRegistryImageWorkflowRun(runs []*github.WorkflowRun, ref, expectedTitle string, minRunID int64, dispatchedAt time.Time) *github.WorkflowRun {
	var matched *github.WorkflowRun

	for _, run := range runs {
		if run == nil || run.GetID() <= minRunID {
			continue
		}
		if run.GetEvent() != "workflow_dispatch" {
			continue
		}
		if !registryImageWorkflowRunMatchesRef(run, ref) {
			continue
		}
		if run.CreatedAt != nil && run.CreatedAt.Time.Before(dispatchedAt.Add(-1*time.Minute)) {
			continue
		}

		if run.GetDisplayTitle() != expectedTitle {
			continue
		}
		if matched == nil || run.GetID() < matched.GetID() {
			matched = run
		}
	}

	return matched
}

func registryImageWorkflowRunMatchesRef(run *github.WorkflowRun, ref string) bool {
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

func newRegistryImageGitHubClient(token string) (*github.Client, *http.Client) {
	httpClient := &http.Client{Timeout: registryImageHTTPTimeout}
	return github.NewClient(httpClient).WithAuthToken(token), httpClient
}

func buildWorkflowPageURL(cfg registryImageWorkflowConfig) string {
	return fmt.Sprintf("https://github.com/%s/%s/actions/workflows/%s", cfg.Owner, cfg.Repo, cfg.Workflow)
}

func buildRegistryImageRunURL(cfg registryImageWorkflowConfig, runID int64, htmlURL string) string {
	if htmlURL != "" {
		return htmlURL
	}

	return fmt.Sprintf("https://github.com/%s/%s/actions/runs/%d", cfg.Owner, cfg.Repo, runID)
}
