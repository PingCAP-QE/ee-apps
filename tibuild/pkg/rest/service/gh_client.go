package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/google/go-github/v61/github"
)

var _ GHClient = (*GitHubClient)(nil)
var sha1regex *regexp.Regexp = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)

type GitHubClient struct{ *github.Client }

// define a struct for the json data:
/*
{
    "branches": [
        {
            "branch": "master",
            "prs": [
                {
                    "number": 57522,
                    "showPrefix": false,
                    "repo": {
                        "name": "tidb",
                        "ownerLogin": "pingcap"
                    },
                    "globalRelayId": "PR_kwDOAoCpQc6CbBGf"
                }
            ]
        }
    ],
    "tags": [
        "v9.0.0-alpha"
    ]
}
*/
type branchesForCommitResponse struct {
	Branches []struct {
		Branch string `json:"branch"`
	}
	Tags []string `json:"tags"`
}

// GetBranchesForCommit implements GHClient.
func (c GitHubClient) GetBranchesForCommit(ctx context.Context, owner string, repo string, commit string) ([]string, error) {
	// check for github owner format with regexp, only support [a-zA-Z0-9_-]
	if !regexp.MustCompile(`^[a-zA-Z0-9_-]+$`).MatchString(owner) {
		return nil, fmt.Errorf("owner %s is not a valid github owner", owner)
	}
	// check for github repo name format with regexp, only support [a-zA-Z0-9_-]
	if !regexp.MustCompile(`^[a-zA-Z0-9_-]+$`).MatchString(repo) {
		return nil, fmt.Errorf("repo %s is not a valid github repo name", repo)
	}
	// check for commit format
	if !sha1regex.MatchString(commit) {
		return nil, fmt.Errorf("commit %s is not a valid sha1", commit)
	}

	rawURL, err := url.JoinPath("https://github.com", owner, repo, "branch_commits", commit)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}

	req.Header.Add("Accept", "application/json")
	res, err := c.Client.Client().Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()

	// parse the res body
	var data branchesForCommitResponse
	// read the response body and unmarshal to `data`
	if err := json.NewDecoder(res.Body).Decode(&data); err != nil {
		return nil, err
	}

	var branches []string
	for _, branch := range data.Branches {
		branches = append(branches, branch.Branch)
	}
	return branches, nil
}

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
