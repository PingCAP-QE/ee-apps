package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
	"github.com/google/go-github/v69/github"
)

type tidbxGitTagMeta struct {
	Author *string                  `json:"author,omitempty"`
	Meta   *hotfix.TiDBxBumpTagMeta `json:"meta,omitempty"`
}

// BumpTagForTidbx creates a hot fix git tag for a GitHub repository.
func (s *hotfixsrvc) BumpTagForTidbx(ctx context.Context, p *hotfix.BumpTagForTidbxPayload) (*hotfix.HotfixTagResult, error) {
	l := s.logger.With().
		Str("repo", p.Repo).
		Str("author", p.Author).
		Logger()

	// Parse repository owner and name
	parts := strings.SplitN(p.Repo, "/", 2)
	if len(parts) != 2 {
		l.Warn().Msg("Invalid repository format, expected 'owner/repo'")
		return nil, &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "invalid repository format, expected 'owner/repo'",
		}
	}
	owner, repo := parts[0], parts[1]

	// Validate that at least one of branch or commit is provided
	if p.Branch == nil && p.Commit == nil {
		l.Warn().Msg("At least one of 'branch' or 'commit' must be provided")
		return nil, &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "at least one of 'branch' or 'commit' must be provided",
		}
	}

	// Step 1: Verify the branch or commit exists
	commitSHA, err := s.verifyAndGetCommit(ctx, owner, repo, p.Branch, p.Commit)
	if err != nil {
		l.Err(err).Msg("Failed to verify commit")
		return nil, err
	}

	l = l.With().Str("commit", commitSHA).Logger()
	l.Info().Msg("Verified commit exists")

	// Step 2: Compute the tag name (and fail if commit already has a tidbx-style tag)
	tagName, err := s.computeNewTagNameForTidbx(ctx, owner, repo, commitSHA)
	if err != nil {
		l.Err(err).Msg("Failed to compute tag name")
		return nil, err
	}

	l = l.With().Str("tag", tagName).Logger()
	l.Info().Msg("Computed tag name")

	// Step 3: Create the tag with author information
	// Step 3.1: prepare the git message with metadata struct.
	gitTagInfo := tidbxGitTagMeta{
		Author: &p.Author,
		Meta:   p.Meta,
	}
	var tagMessage string
	infoBytes, err := json.Marshal(gitTagInfo) //nolint
	if err != nil {
		l.Err(err).Msg("marshal tag metadata message failed.")
		// fallback to normal message.
		tagMessage = fmt.Sprintf("Created hot fix tag on behalf of %s", p.Author)
	} else {
		tagMessage = string(infoBytes)
	}

	// Step 3.2: Create tag
	if err := s.createTag(ctx, owner, repo, tagName, commitSHA, tagMessage); err != nil {
		l.Err(err).Msg("Failed to create tag")
		return nil, err
	}

	l.Info().Msg("Successfully created tag")
	return &hotfix.HotfixTagResult{
		Repo:   p.Repo,
		Commit: commitSHA,
		Tag:    tagName,
		Author: &p.Author,
		Meta:   p.Meta,
	}, nil
}

// QueryTagOfTidbx get the TiDB-X tag information.
func (s *hotfixsrvc) QueryTagOfTidbx(ctx context.Context, p *hotfix.QueryTagOfTidbxPayload) (*hotfix.HotfixTagResult, error) {
	l := s.logger.With().
		Str("repo", p.Repo).
		Str("tag", p.Tag).
		Logger()

	// Parse repository owner and name
	parts := strings.SplitN(p.Repo, "/", 2)
	if len(parts) != 2 {
		l.Warn().Msg("Invalid repository format, expected 'owner/repo'")
		return nil, &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: "invalid repository format, expected 'owner/repo'",
		}
	}
	owner, repo := parts[0], parts[1]

	// 1. Get git tag ref information.
	tagObj, err := s.getTag(ctx, owner, repo, p.Tag)
	if err != nil {
		l.Err(err).Msg("Failed to get tag ref")
		return nil, &hotfix.HTTPError{
			Code:    http.StatusNotFound,
			Message: fmt.Sprintf("failed to get tag ref object: %v", err),
		}
	}
	l.Info().Msg("Successfully get tag ref")
	// 2. Parse fields in git tag message (metadata struct).
	tagMetadata := new(tidbxGitTagMeta)
	if m := strings.TrimSpace(tagObj.GetMessage()); m != "" {
		if err := json.Unmarshal([]byte(m), &tagMetadata); err != nil {
			l.Warn().Err(err).Msg("failed to parse metadata in git tag message")
		}
	}

	ret := &hotfix.HotfixTagResult{
		Repo:   p.Repo,
		Commit: tagObj.GetSHA(),
		Tag:    tagObj.GetTag(),
	}
	if tagMetadata != nil {
		ret.Author = tagMetadata.Author
		ret.Meta = tagMetadata.Meta
	}

	return ret, nil
}

// computeNewTagNameForTidbx computes the next tag name based on existing tags,
// and fails if the provided commit already has a tidbx-style tag.
// Tags follow the pattern vX.Y.Z-nextgen.YYYYMM.N
func (s *hotfixsrvc) computeNewTagNameForTidbx(ctx context.Context, owner, repo, commitSHA string) (string, error) {
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

		// If the tidbx-style tag points to the provided commit, fail fast
		if pattern.MatchString(name) {
			if tag.Commit != nil && tag.Commit.SHA != nil && *tag.Commit.SHA == commitSHA {
				return "", &hotfix.HTTPError{
					Code:    http.StatusBadRequest,
					Message: fmt.Sprintf("commit %s already has existing tidbx-style tag: %s", commitSHA, name),
				}
			}
		}

		matches := pattern.FindStringSubmatch(name)
		if len(matches) == 4 {
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

	// Check if the commit is behind the latest existing tidbx-style tag
	comparison, _, err := s.ghClient.Repositories.CompareCommits(ctx, owner, repo, latest.name, commitSHA, nil)
	if err != nil {
		return "", &hotfix.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to compare commits: %v", err),
		}
	}

	if comparison.GetStatus() == "behind" {
		return "", &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: fmt.Sprintf("commit %s is behind existing tidbx-style tag %s; cannot create new tag on an outdated commit", commitSHA, latest.name),
		}
	}

	// Always increment sequence based on the latest tag's month found in the repository
	return fmt.Sprintf("v%s-nextgen.%s.%d", latest.version, latest.yearMonth, latest.sequence+1), nil
}
