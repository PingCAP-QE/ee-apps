package impl

import (
	"context"
	"fmt"
	"net/http"

	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

// hotfixsrvc service implementation.
type hotfixsrvc struct {
	logger   *zerolog.Logger
	ghClient *github.Client
}

// tagInfo holds information about a parsed tag
type tagInfo struct {
	name      string
	version   string // e.g., "8.5.4"
	yearMonth string // e.g., "202510"
	sequence  int    // e.g., 10
}

// NewHotfix returns the hotfix service implementation.
func NewHotfix(logger *zerolog.Logger, ghClient *github.Client) hotfix.Service {
	return &hotfixsrvc{
		logger:   logger,
		ghClient: ghClient,
	}
}

// verifyAndGetCommit verifies that the branch or commit exists and returns the commit SHA.
// If both branch and commit are provided, it verifies that the commit exists in the branch.
func (s *hotfixsrvc) verifyAndGetCommit(ctx context.Context, owner, repo string, branch, commit *string) (string, error) {
	var commitSHA string

	// Case 1: Only commit is provided
	if commit != nil && branch == nil {
		// Verify the commit exists
		c, _, err := s.ghClient.Repositories.GetCommit(ctx, owner, repo, *commit, nil)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit '%s' not found: %v", *commit, err),
			}
		}
		commitSHA = c.GetSHA()
	}

	// Case 2: Only branch is provided
	if branch != nil && commit == nil {
		// Get the latest commit from the branch
		b, _, err := s.ghClient.Repositories.GetBranch(ctx, owner, repo, *branch, 10)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("branch '%s' not found: %v", *branch, err),
			}
		}
		commitSHA = b.GetCommit().GetSHA()
	}

	// Case 3: Both branch and commit are provided
	if branch != nil && commit != nil {
		// First verify the commit exists
		c, _, err := s.ghClient.Repositories.GetCommit(ctx, owner, repo, *commit, nil)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit '%s' not found: %v", *commit, err),
			}
		}
		commitSHA = c.GetSHA()

		// Then verify the commit exists in the branch by comparing commits
		comparison, _, err := s.ghClient.Repositories.CompareCommits(ctx, owner, repo, *branch, commitSHA, nil)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("failed to compare branch and commit: %v", err),
			}
		}

		// The commit should be in the branch if status is "behind" or "identical"
		// "ahead" means the commit is not in the branch
		status := comparison.GetStatus()
		if status == "ahead" || status == "diverged" {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit '%s' does not exist in branch '%s'", *commit, *branch),
			}
		}
	}

	return commitSHA, nil
}

// createTag creates a git tag with the specified name, commit SHA, and message.
func (s *hotfixsrvc) createTag(ctx context.Context, owner, repo, tagName, commitSHA, message string) error {
	// Create a tag object (annotated tag)
	tagObject := &github.Tag{
		Tag:     github.Ptr(tagName),
		Message: github.Ptr(message),
		Object: &github.GitObject{
			Type: github.Ptr("commit"),
			SHA:  github.Ptr(commitSHA),
		},
	}

	createdTag, _, err := s.ghClient.Git.CreateTag(ctx, owner, repo, tagObject)
	if err != nil {
		return &hotfix.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to create tag object: %v", err),
		}
	}

	// Create a reference to the tag
	ref := &github.Reference{
		Ref: github.Ptr(fmt.Sprintf("refs/tags/%s", tagName)),
		Object: &github.GitObject{
			SHA: createdTag.SHA,
		},
	}

	_, _, err = s.ghClient.Git.CreateRef(ctx, owner, repo, ref)
	if err != nil {
		return &hotfix.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to create tag reference: %v", err),
		}
	}

	return nil
}

func (s *hotfixsrvc) getTag(ctx context.Context, owner, repo, tagName string) (*github.Tag, error) {
	// 1. Get git tag ref information.
	refObj, _, err := s.ghClient.Git.GetRef(ctx, owner, repo, fmt.Sprintf("tags/%s", tagName))
	if err != nil {
		return nil, &hotfix.HTTPError{
			Code:    http.StatusNotFound,
			Message: fmt.Sprintf("failed to get tag ref object: %v", err),
		}
	}

	// 2. Get the tag message
	tagSha := refObj.Object.GetSHA()
	tagObj, _, err := s.ghClient.Git.GetTag(ctx, owner, repo, tagSha)
	if err != nil {
		return nil, &hotfix.HTTPError{
			Code:    http.StatusNotFound,
			Message: fmt.Sprintf("failed to get tag object: %v", err),
		}
	}

	return tagObj, nil
}
