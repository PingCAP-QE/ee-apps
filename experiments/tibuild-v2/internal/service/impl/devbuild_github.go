package impl

import (
	"context"
	"fmt"
	"strings"

	"github.com/google/go-github/v69/github"
)

func getGhRefSha(ctx context.Context, ghClient *github.Client, fullRepo, ref string) string {
	if ghClient == nil {
		return ""
	}

	parts := strings.SplitN(fullRepo, "/", 2)
	if len(parts) != 2 {
		return ""
	}

	owner, repo := parts[0], parts[1]
	branch, _, err := ghClient.Repositories.GetBranch(ctx, owner, repo, ref, 10)
	if err != nil {
		return ""
	}

	return branch.Commit.GetSHA()
}

func newFakeGitHubPushEventPayload(fullRepo, ref, sha string) *github.PushEvent {
	return &github.PushEvent{
		Ref:    github.Ptr(ref),
		After:  github.Ptr(sha),
		Before: github.Ptr("00000000000000000000000000000000000000000"),
		Repo:   newFakeGithubPushRepo(fullRepo),
	}
}

func newFakeGitHubTagCreateEventPayload(fullRepo, ref string) *github.CreateEvent {
	return &github.CreateEvent{
		Ref:     github.Ptr(strings.Replace(ref, "refs/tags/", "", 1)),
		RefType: github.Ptr("tag"),
		Repo:    newFakeGithubRepo(fullRepo),
	}
}

func newFakeGitHubPullRequestPayload(fullRepo, baseRef, headSHA string, number int) *github.PullRequestEvent {
	return &github.PullRequestEvent{
		Action:      github.Ptr("opened"),
		Number:      github.Ptr(number),
		PullRequest: newFakeGithubPullRequest(baseRef, headSHA, number),
		Repo:        newFakeGithubRepo(fullRepo),
	}
}

func newFakeGithubRepo(fullRepo string) *github.Repository {
	return &github.Repository{
		FullName: github.Ptr(fullRepo),
		Name:     github.Ptr(strings.Split(fullRepo, "/")[1]),
		CloneURL: github.Ptr(fmt.Sprintf("https://github.com/%s.git", fullRepo)),
		Owner: &github.User{
			Login: github.Ptr(strings.Split(fullRepo, "/")[0]),
		},
	}
}

func newFakeGithubPushRepo(fullRepo string) *github.PushEventRepository {
	return &github.PushEventRepository{
		FullName: github.Ptr(fullRepo),
		Name:     github.Ptr(strings.Split(fullRepo, "/")[1]),
		CloneURL: github.Ptr(fmt.Sprintf("https://github.com/%s.git", fullRepo)),
		Owner: &github.User{
			Login: github.Ptr(strings.Split(fullRepo, "/")[0]),
		},
	}
}

func newFakeGithubPullRequest(baseRef, headSHA string, number int) *github.PullRequest {
	return &github.PullRequest{
		Number: github.Ptr(number),
		State:  github.Ptr("open"),
		Head: &github.PullRequestBranch{
			SHA: github.Ptr(headSHA),
		},
		Base: &github.PullRequestBranch{
			Ref: github.Ptr(baseRef),
		},
	}
}
