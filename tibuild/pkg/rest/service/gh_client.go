package service

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/google/go-github/github"
)

type GitHubClient struct{ *github.Client }

func GitRefToGHRef(ref string) string {
	if branchName, found := strings.CutPrefix(ref, "branch/"); found {
		return fmt.Sprintf("refs/heads/%s", branchName)
	}
	return ""
}

func (c GitHubClient) GetHash(ctx context.Context, repo GithubRepo, ref string) (string, error) {
	if sha1regex.MatchString(ref) {
		return ref, nil
	}
	gref := GitRefToGHRef(ref)
	if gref == "" {
		return "", fmt.Errorf("bad git ref:%s", ref)
	}
	rt, _, err := c.Git.GetRef(ctx, repo.Owner, repo.Repo, gref)
	if err != nil {
		return "", err
	}
	return *rt.Object.SHA, nil
}

func NewGHClient() GHClient {
	return GitHubClient{github.NewClient(http.DefaultClient)}
}

var sha1regex *regexp.Regexp = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)
