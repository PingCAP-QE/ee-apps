package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
	"github.com/google/go-github/v69/github"
	"golang.org/x/mod/semver"
)

type tidbxGitTagMeta struct {
	Author *string                  `json:"author,omitempty"`
	Meta   *hotfix.TiDBxBumpTagMeta `json:"meta,omitempty"`
}

var (
	legacyTidbxTagPattern       = regexp.MustCompile(`^v(\d+\.\d+\.\d+)-nextgen\.(\d{6})\.(\d+)$`)
	alphaTidbxTagPattern        = regexp.MustCompile(`^(v\d+\.\d+\.\d+)-alpha$`)
	releaseNextgenBranchPattern = regexp.MustCompile(`^release-nextgen-(\d{4})(\d{2})(?:\d{2})?$`)
	releaseNextgenDailyBranch   = regexp.MustCompile(`^release-nextgen-\d{8}$`)
)

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
	tagName, err := s.computeNewTagNameForTidbx(ctx, owner, repo, commitSHA, p.Branch)
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
	queryTag := normalizeTidbxQueryTag(p.Tag)

	// 1. Get git tag ref information.
	tagObj, err := s.getTag(ctx, owner, repo, queryTag)
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

// computeNewTagNameForTidbx computes the next tag name based on existing tags
// and fails if the provided commit already has a tidbx-style tag.
func (s *hotfixsrvc) computeNewTagNameForTidbx(ctx context.Context, owner, repo, commitSHA string, branch *string) (string, error) {
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

	// Parse and filter three kinds of tags:
	// 1) new GA tags: vX.Y.Z
	// 2) new alpha tags: vX.Y.Z-alpha
	// 3) legacy tags: vX.Y.Z-nextgen.YYYYMM.N
	var legacyTags []tagInfo
	var newTags []string
	var alphaTags []string

	for _, tag := range allTags {
		name := tag.GetName()
		baseAlphaTag, isAlphaTag := parseTidbxAlphaTag(name)

		// Keep "already tagged" guard for GA and legacy tags.
		// For alpha tags, we allow promotion on the same commit (vX.Y.Z-alpha -> vX.Y.Z).
		if (legacyTidbxTagPattern.MatchString(name) || isNewTidbxGitTag(name)) &&
			tag.Commit != nil && tag.Commit.SHA != nil && *tag.Commit.SHA == commitSHA {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit %s already has existing tidbx-style tag: %s", commitSHA, name),
			}
		}

		if isNewTidbxGitTag(name) {
			newTags = append(newTags, name)
			continue
		}

		if isAlphaTag {
			alphaTags = append(alphaTags, baseAlphaTag)
			continue
		}

		matches := legacyTidbxTagPattern.FindStringSubmatch(name)
		if len(matches) == 4 {
			seq, err := strconv.Atoi(matches[3])
			if err != nil {
				continue
			}
			legacyTags = append(legacyTags, tagInfo{
				name:      name,
				version:   matches[1],
				yearMonth: matches[2],
				sequence:  seq,
			})
		}
	}

	headBranch := ""
	if branch != nil {
		headBranch = *branch
	} else {
		headBranch = s.getLatestNextgenReleaseBranchForCommit(ctx, owner, repo, commitSHA)
	}

	if len(newTags) > 0 {
		sort.Slice(newTags, func(i, j int) bool {
			return semver.Compare(newTags[i], newTags[j]) > 0
		})

		latest := newTags[0]
		comparison, _, err := s.ghClient.Repositories.CompareCommits(ctx, owner, repo, latest, commitSHA, nil)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusInternalServerError,
				Message: fmt.Sprintf("failed to compare commits: %v", err),
			}
		}
		if comparison.GetStatus() == "behind" {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit %s is behind existing tidbx-style tag %s; cannot create new tag on an outdated commit", commitSHA, latest),
			}
		}

		parts := strings.Split(strings.TrimPrefix(latest, "v"), ".")
		patch, err := strconv.Atoi(parts[2])
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusInternalServerError,
				Message: fmt.Sprintf("failed to parse patch from tag %s: %v", latest, err),
			}
		}
		return fmt.Sprintf("v%s.%s.%d", parts[0], parts[1], patch+1), nil
	}

	// If there is no GA new-style tag, but there is alpha tag, promote the latest
	// alpha tag to its first GA tag (vX.Y.Z-alpha -> vX.Y.Z).
	if len(alphaTags) > 0 {
		sort.Slice(alphaTags, func(i, j int) bool {
			return semver.Compare(alphaTags[i], alphaTags[j]) > 0
		})

		latestAlphaBase := alphaTags[0]
		latestAlphaTag := latestAlphaBase + "-alpha"
		comparison, _, err := s.ghClient.Repositories.CompareCommits(ctx, owner, repo, latestAlphaTag, commitSHA, nil)
		if err != nil {
			return "", &hotfix.HTTPError{
				Code:    http.StatusInternalServerError,
				Message: fmt.Sprintf("failed to compare commits: %v", err),
			}
		}
		if comparison.GetStatus() == "behind" {
			return "", &hotfix.HTTPError{
				Code:    http.StatusBadRequest,
				Message: fmt.Sprintf("commit %s is behind existing tidbx-style tag %s; cannot create new tag on an outdated commit", commitSHA, latestAlphaTag),
			}
		}

		return latestAlphaBase, nil
	}

	// If only legacy tags exist and the commit's head branch is legacy style, keep legacy bump behavior.
	if len(legacyTags) > 0 && releaseNextgenDailyBranch.MatchString(headBranch) {
		sort.Slice(legacyTags, func(i, j int) bool {
			if legacyTags[i].yearMonth != legacyTags[j].yearMonth {
				return legacyTags[i].yearMonth > legacyTags[j].yearMonth
			}
			return legacyTags[i].sequence > legacyTags[j].sequence
		})

		latest := legacyTags[0]

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

		// Always increment sequence based on the latest tag's month found in the repository.
		return fmt.Sprintf("v%s-nextgen.%s.%d", latest.version, latest.yearMonth, latest.sequence+1), nil
	}

	// If there are no tags, and caller provides a release-nextgen branch in new style,
	// bootstrap the first patch version: vYY.M.0.
	if headBranch != "" {
		if bootstrapTag, ok := buildBootstrapTagFromReleaseBranch(headBranch); ok {
			return bootstrapTag, nil
		}
	}

	return "", &hotfix.HTTPError{
		Code:    http.StatusBadRequest,
		Message: "no existing tags found matching new-style/legacy patterns, and branch does not match bootstrap rule release-nextgen-YYYYMM[/DD]",
	}
}

func (s *hotfixsrvc) getLatestNextgenReleaseBranchForCommit(ctx context.Context, owner, repo, commitSHA string) string {
	ret, _, err := s.ghClient.Repositories.ListBranchesHeadCommit(ctx, owner, repo, commitSHA)
	if err != nil {
		s.logger.Err(err).Msg("fetch commit's head branches failed")
	}

	var branchNames []string
	for _, b := range ret {
		branchNames = append(branchNames, b.GetName())
	}

	releaseBranches := slices.DeleteFunc(branchNames, func(b string) bool {
		return releaseNextgenBranchPattern.MatchString(b)
	})
	if len(releaseBranches) == 0 {
		return ""
	}
	return slices.Max(releaseBranches)
}

func parseTidbxAlphaTag(tag string) (string, bool) {
	matches := alphaTidbxTagPattern.FindStringSubmatch(tag)
	if len(matches) != 2 {
		return "", false
	}
	base := matches[1]
	if !isNewTidbxGitTag(base) {
		return "", false
	}

	return base, true
}

func buildBootstrapTagFromReleaseBranch(branch string) (string, bool) {
	matches := releaseNextgenBranchPattern.FindStringSubmatch(branch)
	if len(matches) != 3 {
		return "", false
	}

	year, err := strconv.Atoi(matches[1])
	if err != nil {
		return "", false
	}
	month, err := strconv.Atoi(matches[2])
	if err != nil || month < 1 || month > 12 {
		return "", false
	}

	shortYear := year % 100
	if shortYear <= 25 {
		return "", false
	}

	return fmt.Sprintf("v%d.%d.0", shortYear, month), true
}
