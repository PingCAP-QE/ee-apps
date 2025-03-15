package impl

import (
	"context"
	"strings"

	"github.com/google/go-github/v69/github"
)

func getGhRefSha(ctx context.Context, ghClient *github.Client, fullRepo, ref string) string {
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
