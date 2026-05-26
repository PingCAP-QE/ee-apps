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

type tidbxTagPatchBumper func(lastestTag string) (string, error)

var (
	legacyTidbxTagPattern       = regexp.MustCompile(`^v(\d+\.\d+\.\d+)-nextgen\.(\d{6})\.(\d+)$`)
	alphaTidbxTagPattern        = regexp.MustCompile(`^(v\d+\.\d+\.\d+)-alpha$`)
	releaseNextgenBranchPattern = regexp.MustCompile(`^release-nextgen-(\d{4})(\d{2})(?:\d{2})?$`)
	releaseNextgenDailyBranch   = regexp.MustCompile(`^release-nextgen-\d{8}$`)
	releaseNextgenMonthlyBranch = regexp.MustCompile(`^release-nextgen-\d{6}$`)
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
		b, err := s.getLatestNextgenReleaseBranchForCommit(ctx, owner, repo, commitSHA)
		if err != nil {
			return "", err
		}
		headBranch = b
	}

	if releaseNextgenMonthlyBranch.MatchString(headBranch) {
		if len(newTags) > 0 {
			sort.Slice(newTags, func(i, j int) bool {
				return semver.Compare(newTags[i], newTags[j]) > 0
			})

			return s.computeNextTagByCompareCommits(ctx, owner, repo, commitSHA, newTags[0], newStyleTidbxTagGenerator)
		}

		// If there is no GA new-style tag, but there is alpha tag, promote the latest
		// alpha tag to its first GA tag (vX.Y.Z-alpha -> vX.Y.Z).
		if len(alphaTags) > 0 {
			sort.Slice(alphaTags, func(i, j int) bool {
				return semver.Compare(alphaTags[i], alphaTags[j]) > 0
			})

			return s.computeNextTagByCompareCommits(ctx, owner, repo, commitSHA, alphaTags[0]+"-alpha", newStyleTidbxTagGeneratorFromAlphaTag)
		}
	}

	// If only legacy tags exist and the commit's head branch is legacy style, keep legacy bump behavior.
	if releaseNextgenDailyBranch.MatchString(headBranch) && len(legacyTags) > 0 {
		sort.Slice(legacyTags, func(i, j int) bool {
			if legacyTags[i].yearMonth != legacyTags[j].yearMonth {
				return legacyTags[i].yearMonth > legacyTags[j].yearMonth
			}
			return legacyTags[i].sequence > legacyTags[j].sequence
		})

		return s.computeNextTagByCompareCommits(ctx, owner, repo, commitSHA, legacyTags[0].name, legacyTidbxTagGenerator)
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

// computeNextTagByCompareCommits compares the provided commit against the latest
// existing tag. If the commit is ahead, it uses the given generator to produce the
// next tag name. If identical, behind, or diverged, it returns an appropriate error.
func (s *hotfixsrvc) computeNextTagByCompareCommits(ctx context.Context, owner, repo, commitSHA, lastTag string, aheadFn tidbxTagPatchBumper) (string, error) {
	comparison, _, err := s.ghClient.Repositories.CompareCommits(ctx, owner, repo, lastTag, commitSHA, nil)
	if err != nil {
		return "", &hotfix.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: fmt.Sprintf("failed to compare commits: %v", err),
		}
	}
	switch status := comparison.GetStatus(); status {
	case "identical":
		errMsg := fmt.Sprintf("commit %s is identical with existing tidbx-style tag %s. We can not create a new tag on it.", commitSHA, lastTag)
		s.logger.Warn().Msg(errMsg)
		return "", &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: errMsg,
		}
	case "behind":
		errMsg := fmt.Sprintf("commit %s is behind existing tidbx-style tag %s; cannot create new tag on an outdated commit", commitSHA, lastTag)
		s.logger.Warn().Msg(errMsg)
		return "", &hotfix.HTTPError{
			Code:    http.StatusBadRequest,
			Message: errMsg,
		}
	case "diverged":
		errMsg := fmt.Sprintf("commit %s is diverged with tidbx-style tag %s; please find the releaser to address it", commitSHA, lastTag)
		s.logger.Error().Msg(errMsg)
		return "", &hotfix.HTTPError{
			Code:    http.StatusConflict,
			Message: errMsg,
		}
	case "ahead":
		newTag, err := aheadFn(lastTag)
		if err != nil {
			errMsg := fmt.Sprintf("failed to generate next tag from %s: %v", lastTag, err)
			s.logger.Error().Msg(errMsg)
			return "", &hotfix.HTTPError{
				Code:    http.StatusInternalServerError,
				Message: errMsg,
			}
		}
		return newTag, nil
	default:
		errMsg := fmt.Sprintf("unknown compare status: '%s'", status)
		s.logger.Error().Msg(errMsg)
		return "", &hotfix.HTTPError{
			Code:    http.StatusInternalServerError,
			Message: errMsg,
		}
	}
}

func (s *hotfixsrvc) getLatestNextgenReleaseBranchForCommit(ctx context.Context, owner, repo, commitSHA string) (string, error) {
	branchNames, err := s.getBranchesContainingCommit(ctx, owner, repo, commitSHA)
	if err != nil {
		return "", err
	}

	releaseBranches := slices.DeleteFunc(branchNames, func(b string) bool {
		return !releaseNextgenBranchPattern.MatchString(b)
	})

	if len(releaseBranches) == 0 {
		return "", nil
	}
	return slices.Max(releaseBranches), nil
}

func (s *hotfixsrvc) getBranchesContainingCommit(ctx context.Context, owner, repo, commitSHA string) ([]string, error) {
	u := fmt.Sprintf("https://%s/%s/%s/branch_commits/%s", "github.com", owner, repo, commitSHA)
	req, err := s.ghClient.NewRequest("GET", u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")

	var ret struct {
		Branches []struct {
			Branch string `json:"branch,omitempty"`
		}
	}
	if _, err := s.ghClient.Do(ctx, req, &ret); err != nil {
		s.logger.Err(err).Msg("fetch branches which contains the given commit failed")
		return nil, err
	}

	var branchNames []string
	for _, b := range ret.Branches {
		branchNames = append(branchNames, b.Branch)
	}

	return branchNames, nil
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

func newStyleTidbxTagGenerator(latest string) (string, error) {
	parts := strings.Split(strings.TrimPrefix(latest, "v"), ".")
	patch, err := strconv.Atoi(parts[2])
	if err != nil {
		return "", err
	}

	return fmt.Sprintf("v%s.%s.%d", parts[0], parts[1], patch+1), nil
}

func newStyleTidbxTagGeneratorFromAlphaTag(latestAlpha string) (string, error) {
	tag, ok := parseTidbxAlphaTag(latestAlpha)
	if !ok {
		return "", fmt.Errorf("invalid alpha tag: %s", latestAlpha)
	}
	return tag, nil
}

func legacyTidbxTagGenerator(name string) (string, error) {
	matches := legacyTidbxTagPattern.FindStringSubmatch(name)
	if len(matches) != 4 {
		return "", fmt.Errorf("tag %s does not match legacy tidbx tag pattern", name)
	}
	seq, err := strconv.Atoi(matches[3])
	if err != nil {
		return "", err
	}

	semVer := matches[1]
	yearMonth := matches[2]

	return fmt.Sprintf("v%s-nextgen.%s.%d", semVer, yearMonth, seq+1), nil
}
