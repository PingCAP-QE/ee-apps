package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/client/githubauth"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/google/go-github/v93/github"
)

// NewGitHubClient creates a new GitHub client with the provided static token.
// It is kept for callers that do not have a full config object.
func NewGitHubClient(token string) *github.Client {
	return NewGitHubClientWithConfig(config.Github{Token: token})
}

// NewGitHubClientWithConfig creates a GitHub client that prefers
// repository-scoped tokens from the broker and falls back to the static token.
func NewGitHubClientWithConfig(cfg config.Github) *github.Client {
	transport := githubauth.NewTransport(githubauth.Config{
		BrokerURL:     cfg.BrokerURL,
		TokenFile:     cfg.BrokerTokenFile,
		FallbackToken: cfg.Token,
	})
	httpClient := &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
	}
	opts := []github.ClientOptionsFunc{github.WithHTTPClient(httpClient)}

	// An explicit API base URL lets tests point at an httptest server and
	// supports GitHub Enterprise deployments. Invalid values fall back to the
	// default endpoint.
	if cfg.APIURL != "" {
		if u, err := url.Parse(cfg.APIURL); err == nil && u.Scheme != "" && u.Host != "" {
			baseURL := u.String()
			opts = append(opts, github.WithURLs(&baseURL, nil))
		}
	}

	client, err := github.NewClient(opts...)
	if err != nil {
		// The options above are pre-validated; retry with the public endpoint
		// so a bad override cannot take the service down.
		client, _ = github.NewClient(github.WithHTTPClient(httpClient))
	}

	return client
}

// gitRefInfo is the resolved form of a devbuild gitRef.
type gitRefInfo struct {
	// Ref is the fully qualified git ref carried by the synthetic event:
	// refs/heads/<branch>, refs/tags/<tag>, or the commit SHA.
	Ref string
	// SHA is the commit the build must check out.
	SHA string
	// BaseRef is the base branch of a pull request (pull/<n> only).
	BaseRef string
}

// gitRefNotFoundError reports a ref GitHub says does not exist. It maps to HTTP
// 400 because the request body references a nonexistent object.
type gitRefNotFoundError struct {
	message string
}

func (e *gitRefNotFoundError) Error() string { return e.message }

// githubUpstreamError reports a GitHub failure that is not a "not found"
// (auth, rate limit, 5xx, network). It maps to HTTP 500 and must never degrade
// to an empty revision.
type githubUpstreamError struct {
	repo   string
	ref    string
	status int
	err    error
}

func (e *githubUpstreamError) Error() string {
	if e.status != 0 {
		return fmt.Sprintf("github error %d while resolving %q in %s: %v", e.status, e.ref, e.repo, e.err)
	}
	return fmt.Sprintf("failed to resolve %q in %s: %v", e.ref, e.repo, e.err)
}

func (e *githubUpstreamError) Unwrap() error { return e.err }

// getGitRefInfo resolves a devbuild gitRef against GitHub exactly once and
// returns the ref, the commit SHA and (for pull requests) the base branch.
// A missing ref is reported as *gitRefNotFoundError; every other failure is
// reported as *githubUpstreamError so callers can pick 400 vs 500.
func getGitRefInfo(ctx context.Context, ghClient *github.Client, fullRepo, reqGitRef string) (gitRefInfo, error) {
	if ghClient == nil {
		return gitRefInfo{}, &githubUpstreamError{
			repo: fullRepo,
			ref:  reqGitRef,
			err:  errors.New("github client is not configured"),
		}
	}

	parts := strings.SplitN(fullRepo, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return gitRefInfo{}, &gitRefNotFoundError{
			message: fmt.Sprintf("invalid github repo %q", fullRepo),
		}
	}
	owner, repo := parts[0], parts[1]

	switch {
	case strings.HasPrefix(reqGitRef, "branch/"):
		branchName := strings.TrimPrefix(reqGitRef, "branch/")
		if branchName == "" {
			return gitRefInfo{}, &gitRefNotFoundError{
				message: fmt.Sprintf("branch name is empty in %s", fullRepo),
			}
		}

		// Use git/ref instead of Repositories.GetBranch: the latter surfaces a
		// plain "unexpected status code" error without a *github.ErrorResponse,
		// which makes 404 and upstream failures indistinguishable.
		ref, _, err := ghClient.Git.GetRef(ctx, owner, repo, "refs/heads/"+branchName)
		if err != nil {
			return gitRefInfo{}, classifyGitRefError(
				err, fullRepo, reqGitRef,
				fmt.Sprintf("branch %q not found in %s", branchName, fullRepo),
			)
		}
		sha := ref.GetObject().GetSHA()
		if sha == "" {
			return gitRefInfo{}, emptySHAError(fullRepo, reqGitRef)
		}
		return gitRefInfo{Ref: "refs/heads/" + branchName, SHA: sha}, nil

	case strings.HasPrefix(reqGitRef, "tag/"):
		tagName := strings.TrimPrefix(reqGitRef, "tag/")
		if tagName == "" {
			return gitRefInfo{}, &gitRefNotFoundError{
				message: fmt.Sprintf("tag name is empty in %s", fullRepo),
			}
		}

		ref, _, err := ghClient.Git.GetRef(ctx, owner, repo, "refs/tags/"+tagName)
		if err != nil {
			return gitRefInfo{}, classifyGitRefError(
				err, fullRepo, reqGitRef,
				fmt.Sprintf("tag %q not found in %s", tagName, fullRepo),
			)
		}
		// Annotated tags point at tag objects rather than commits. Peel them.
		object := ref.GetObject()
		for depth := 0; object != nil && object.GetType() == "tag"; depth++ {
			if depth >= 8 {
				return gitRefInfo{}, &githubUpstreamError{repo: fullRepo, ref: reqGitRef, err: errors.New("annotated tag nesting exceeds 8 levels")}
			}
			tag, _, err := ghClient.Git.GetTag(ctx, owner, repo, object.GetSHA())
			if err != nil {
				return gitRefInfo{}, classifyGitRefError(err, fullRepo, reqGitRef, fmt.Sprintf("tag %q not found in %s", tagName, fullRepo))
			}
			object = tag.GetObject()
		}
		sha := object.GetSHA()
		if sha == "" {
			return gitRefInfo{}, emptySHAError(fullRepo, reqGitRef)
		}
		return gitRefInfo{Ref: "refs/tags/" + tagName, SHA: sha}, nil

	case strings.HasPrefix(reqGitRef, "pull/"):
		numberStr := strings.TrimPrefix(reqGitRef, "pull/")
		number, err := strconv.Atoi(numberStr)
		if err != nil || number <= 0 {
			return gitRefInfo{}, &gitRefNotFoundError{
				message: fmt.Sprintf("invalid pull request number %q in %s", numberStr, fullRepo),
			}
		}

		pr, _, err := ghClient.PullRequests.Get(ctx, owner, repo, number)
		if err != nil {
			return gitRefInfo{}, classifyGitRefError(
				err, fullRepo, reqGitRef,
				fmt.Sprintf("pull request #%d not found in %s", number, fullRepo),
			)
		}
		sha := pr.GetHead().GetSHA()
		if sha == "" {
			return gitRefInfo{}, emptySHAError(fullRepo, reqGitRef)
		}
		base := pr.GetBase().GetRef()
		return gitRefInfo{Ref: "refs/heads/" + base, SHA: sha, BaseRef: base}, nil

	case strings.HasPrefix(reqGitRef, "commit/"), isHex(reqGitRef):
		sha := strings.TrimPrefix(reqGitRef, "commit/")
		if !isHex(sha) {
			return gitRefInfo{}, &gitRefNotFoundError{
				message: fmt.Sprintf("commit %q is not a 40-character SHA in %s", sha, fullRepo),
			}
		}

		commit, _, err := ghClient.Repositories.GetCommit(ctx, owner, repo, sha, nil)
		if err != nil {
			return gitRefInfo{}, classifyGitRefError(
				err, fullRepo, reqGitRef,
				fmt.Sprintf("commit %q not found in %s", sha, fullRepo),
			)
		}
		resolved := commit.GetSHA()
		if resolved == "" {
			resolved = sha
		}
		return gitRefInfo{Ref: resolved, SHA: resolved}, nil
	}

	return gitRefInfo{}, &gitRefNotFoundError{
		message: fmt.Sprintf("unsupported git ref %q", reqGitRef),
	}
}

// classifyGitRefError turns a go-github error into a not-found or upstream
// error. Only an explicit HTTP 404 is treated as "does not exist".
func classifyGitRefError(err error, repo, ref, notFoundMessage string) error {
	var responseErr *github.ErrorResponse
	if errors.As(err, &responseErr) && responseErr.Response != nil && responseErr.Response.StatusCode == http.StatusNotFound {
		return &gitRefNotFoundError{message: notFoundMessage}
	}
	return &githubUpstreamError{repo: repo, ref: ref, status: githubStatusCode(err), err: err}
}

func githubStatusCode(err error) int {
	var responseErr *github.ErrorResponse
	if errors.As(err, &responseErr) && responseErr.Response != nil {
		return responseErr.Response.StatusCode
	}
	return 0
}

func emptySHAError(repo, ref string) error {
	return &githubUpstreamError{
		repo: repo,
		ref:  ref,
		err:  errors.New("github returned an empty commit sha"),
	}
}

func newFakeGitHubPushEventPayload(fullRepo, ref, sha string) *github.PushEvent {
	return &github.PushEvent{
		Ref:    new(ref),
		After:  new(sha),
		Before: new("00000000000000000000000000000000000000000"),
		Repo:   newFakeGithubPushRepo(fullRepo),
	}
}

func newFakeGitHubTagCreateEventPayload(fullRepo, ref string) *github.CreateEvent {
	return &github.CreateEvent{
		Ref:     new(strings.Replace(ref, "refs/tags/", "", 1)),
		RefType: new("tag"),
		Repo:    newFakeGithubRepo(fullRepo),
	}
}

func newFakeGitHubPullRequestPayload(fullRepo, baseRef, headSHA string, number int) *github.PullRequestEvent {
	return &github.PullRequestEvent{
		Action:      new("opened"),
		Number:      new(number),
		PullRequest: newFakeGithubPullRequest(baseRef, headSHA, number),
		Repo:        newFakeGithubRepo(fullRepo),
	}
}

func newFakeGithubRepo(fullRepo string) *github.Repository {
	return &github.Repository{
		FullName: new(fullRepo),
		Name:     new(strings.Split(fullRepo, "/")[1]),
		CloneURL: new(fmt.Sprintf("https://github.com/%s.git", fullRepo)),
		Owner: &github.User{
			Login: new(strings.Split(fullRepo, "/")[0]),
		},
	}
}

func newFakeGithubPushRepo(fullRepo string) *github.PushEventRepository {
	return &github.PushEventRepository{
		FullName: new(fullRepo),
		Name:     new(strings.Split(fullRepo, "/")[1]),
		CloneURL: new(fmt.Sprintf("https://github.com/%s.git", fullRepo)),
		Owner: &github.User{
			Login: new(strings.Split(fullRepo, "/")[0]),
		},
	}
}

func newFakeGithubPullRequest(baseRef, headSHA string, number int) *github.PullRequest {
	return &github.PullRequest{
		Number: new(number),
		State:  new("open"),
		Head: &github.PullRequestBranch{
			SHA: new(headSHA),
		},
		Base: &github.PullRequestBranch{
			Ref: new(baseRef),
		},
	}
}
