package impl

import (
	"context"
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

func getGhRefAndSha(ctx context.Context, ghClient *github.Client, fullRepo, reqGitRef string) (string, string) {
	if ghClient == nil {
		return "", ""
	}

	parts := strings.SplitN(fullRepo, "/", 2)
	if len(parts) != 2 {
		return "", ""
	}

	owner, repo := parts[0], parts[1]

	switch {
	case strings.HasPrefix(reqGitRef, "branch/"):
		branchName := strings.TrimPrefix(reqGitRef, "branch/")
		branch, _, err := ghClient.Repositories.GetBranch(ctx, owner, repo, branchName, 10)
		if err != nil {
			return "", ""
		}
		return strings.Join([]string{"refs", "heads", branchName}, "/"), branch.Commit.GetSHA()

	case strings.HasPrefix(reqGitRef, "tag/"):
		tagName := strings.TrimPrefix(reqGitRef, "tag/")
		ref, _, err := ghClient.Git.GetRef(ctx, owner, repo, "refs/tags/"+tagName)
		if err != nil {
			return "", ""
		}
		return strings.Join([]string{"refs", "tags", tagName}, "/"), ref.Object.GetSHA()

	case strings.HasPrefix(reqGitRef, "commit/"):
		sha := strings.TrimPrefix(reqGitRef, "commit/")
		if len(sha) != 40 || !isHex(sha) {
			return "", ""
		}
		return sha, sha

	case strings.HasPrefix(reqGitRef, "pull/"):
		prNumberStr := strings.TrimPrefix(reqGitRef, "pull/")
		prNumber, err := strconv.Atoi(prNumberStr)
		if err != nil {
			return "", ""
		}
		pr, _, err := ghClient.PullRequests.Get(ctx, owner, repo, prNumber)
		if err != nil {
			return "", ""
		}
		return *pr.Base.Ref, pr.Head.GetSHA()
	}

	// Fallback: treat raw 40-char hex as a commit SHA for convenience.
	if isHex(reqGitRef) {
		return reqGitRef, reqGitRef
	}

	return "", ""
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
