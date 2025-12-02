package impl

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

// hotfixsrvc service implementation.
type hotfixsrvc struct {
	logger   *zerolog.Logger
	ghClient *github.Client
}

// NewHotfix returns the hotfix service implementation.
func NewHotfix(logger *zerolog.Logger, ghClient *github.Client) hotfix.Service {
	return &hotfixsrvc{
		logger:   logger,
		ghClient: ghClient,
	}
}

// CreateTag creates a hot fix git tag for a GitHub repository.
func (s *hotfixsrvc) CreateTag(ctx context.Context, p *hotfix.CreateTagPayload) (*hotfix.HotfixTagResult, error) {
	l := s.logger.With().
		Str("repo", p.Repo).
		Str("author", p.Author).
		Logger()

	// Parse repository owner and name
	parts := strings.SplitN(p.Repo, "/", 2)
	if len(parts) != 2 {
		return nil, &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "invalid repository format, expected 'owner/repo'",
		}
	}
	owner, repo := parts[0], parts[1]

	// Validate that at least one of branch or commit is provided
	if p.Branch == nil && p.Commit == nil {
		return nil, &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "at least one of 'branch' or 'commit' must be provided",
		}
	}

	// Step 1: Verify the branch or commit exists
	commitSHA, err := s.verifyAndGetCommit(ctx, owner, repo, p.Branch, p.Commit)
	if err != nil {
		return nil, err
	}

	l = l.With().Str("commit", commitSHA).Logger()
	l.Info().Msg("Verified commit exists")

	// Step 2: Compute the tag name
	tagName, err := s.computeTagName(ctx, owner, repo)
	if err != nil {
		return nil, err
	}

	l = l.With().Str("tag", tagName).Logger()
	l.Info().Msg("Computed tag name")

	// Step 3: Create the tag with author information
	tagMessage := fmt.Sprintf("Hot fix tag created by %s", p.Author)
	if err := s.createTag(ctx, owner, repo, tagName, commitSHA, tagMessage); err != nil {
		return nil, err
	}

	l.Info().Msg("Successfully created tag")

	return &hotfix.HotfixTagResult{
		Repo:   p.Repo,
		Commit: commitSHA,
		Tag:    tagName,
	}, nil
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

// tagInfo holds information about a parsed tag
type tagInfo struct {
	name      string
	version   string // e.g., "8.5.4"
	yearMonth string // e.g., "202510"
	sequence  int    // e.g., 10
}

// computeTagName computes the next tag name based on existing tags.
// Tags follow the pattern vX.Y.Z-nextgen.YYYYMM.N
func (s *hotfixsrvc) computeTagName(ctx context.Context, owner, repo string) (string, error) {
	// Get all tags from the repository
	var allTags []*github.RepositoryTag
	opts := &github.ListOptions{PerPage: 100}

	for {
		tags, resp, err := s.ghClient.Repositories.ListTags(ctx, owner, repo, opts)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusInternalServerError,
				Message: fmt.Sprintf("failed to list tags: %v", err),
			}
		}
		allTags = append(allTags, tags...)
		if resp.NextPage == 0 {
			break
		}
		opts.Page = resp.NextPage
	}

	// Parse and filter tags matching the pattern vX.Y.Z-nextgen.YYYYMM.N
	pattern := regexp.MustCompile(`^v(\d+\.\d+\.\d+)-nextgen\.(\d{6})\.(\d+)$`)
	var matchingTags []tagInfo

	for _, tag := range allTags {
		name := tag.GetName()
		matches := pattern.FindStringSubmatch(name)
		if matches != nil && len(matches) == 4 {
			seq, err := strconv.Atoi(matches[3])
			if err != nil {
				continue
			}
			matchingTags = append(matchingTags, tagInfo{
				name:      name,
				version:   matches[1],
				yearMonth: matches[2],
				sequence:  seq,
			})
		}
	}

	// Get current year and month
	now := time.Now()
	currentYearMonth := fmt.Sprintf("%04d%02d", now.Year(), now.Month())

	// If no matching tags exist, we cannot determine the version to use
	if len(matchingTags) == 0 {
		return "", &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "no existing tags found matching pattern vX.Y.Z-nextgen.YYYYMM.N, cannot determine version to use",
		}
	}

	// Sort tags to find the latest one
	// First by yearMonth (descending), then by sequence (descending)
	sort.Slice(matchingTags, func(i, j int) bool {
		if matchingTags[i].yearMonth != matchingTags[j].yearMonth {
			return matchingTags[i].yearMonth > matchingTags[j].yearMonth
		}
		return matchingTags[i].sequence > matchingTags[j].sequence
	})

	latest := matchingTags[0]

	// If the latest tag is from the current month, increment the sequence
	if latest.yearMonth == currentYearMonth {
		return fmt.Sprintf("v%s-nextgen.%s.%d", latest.version, latest.yearMonth, latest.sequence+1), nil
	}

	// If the latest tag is from a previous month, start a new sequence with 1
	return fmt.Sprintf("v%s-nextgen.%s.1", latest.version, currentYearMonth), nil
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
