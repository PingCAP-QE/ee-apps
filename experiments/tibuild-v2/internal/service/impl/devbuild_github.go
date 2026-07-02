package impl

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/google/go-github/v69/github"
)

// NewGitHubClient creates a new GitHub client with the provided token.
func NewGitHubClient(token string) *github.Client {
	if token == "" {
		return github.NewClient(nil)
	}
	return github.NewClient(nil).WithAuthToken(token)
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
