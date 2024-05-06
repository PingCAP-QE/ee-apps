package service

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/google/go-github/v61/github"
)

var _ GHClient = (*GitHubClient)(nil)
var sha1regex *regexp.Regexp = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)

type GitHubClient struct{ *github.Client }

func (c GitHubClient) GetHash(ctx context.Context, owner, repo, ref string) (string, error) {
	if sha1regex.MatchString(ref) {
		return ref, nil
	}
	gref := convertParamGitRefToGitHubRef(ref)
	if gref == "" {
		return "", fmt.Errorf("bad git ref:%s", ref)
	}
	rt, _, err := c.Git.GetRef(ctx, owner, repo, gref)
	if err != nil {
		return "", err
	}
	return *rt.Object.SHA, nil
}

func (c GitHubClient) GetPullRequestInfo(ctx context.Context, owner, repo string, prNum int) (*github.PullRequest, error) {
	pr, _, err := c.PullRequests.Get(ctx, owner, repo, prNum)
	return pr, err
}

func NewGHClient(token string) GHClient {
	client := github.NewClient(http.DefaultClient)
	if token != "" {
		client = client.WithAuthToken(token)
	}
	return GitHubClient{client}
}

func convertParamGitRefToGitHubRef(ref string) string {
	switch {
	case strings.HasPrefix(ref, "branch/"):
		return strings.Replace(ref, "branch/", "refs/heads/", 1)
	case strings.HasPrefix(ref, "tag/"):
		return strings.Replace(ref, "tag/", "refs/tags/", 1)
	case strings.HasPrefix(ref, "pull/"), strings.HasPrefix(ref, "pr/"):
		return strings.Join([]string{strings.Replace(ref, "pull/", "refs/pulls/", 1), "head"}, "/")
	default:
		return ""
	}
}
