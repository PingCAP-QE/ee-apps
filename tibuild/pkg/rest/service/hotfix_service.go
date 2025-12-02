package service

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

type RepoHeadCreator interface {
	CreateBranchFromTag(ctx context.Context, repo GithubRepo, branch string, tag string) error
	CreateTagFromBranch(ctx context.Context, repo GithubRepo, tag string, branch string) error
	GetCommitSHA(ctx context.Context, repo GithubRepo, ref string) (string, error)
	ListTags(ctx context.Context, repo GithubRepo) ([]string, error)
	CreateAnnotatedTag(ctx context.Context, repo GithubRepo, tag string, commit string, message string) error
	GetBranchesForCommit(ctx context.Context, repo GithubRepo, commit string) ([]string, error)
}

type repoService struct {
	now           func() time.Time
	branchCreator RepoHeadCreator
}

var _ HotfixService = repoService{}

const FormatYYYYMMDD = "20060102"

func (r repoService) CreateBranch(ctx context.Context, req BranchCreateReq) (resp *BranchCreateResp, err error) {
	repo := prodToRepoMap[req.Prod]
	if repo == nil {
		return nil, fmt.Errorf("unknown product")
	}
	branchName := BranchName(req.BaseVersion, r.now().Format(FormatYYYYMMDD))
	if branchName == "" {
		return nil, fmt.Errorf("bad branch name, check if tag correct")
	}
	err = r.branchCreator.CreateBranchFromTag(ctx, *repo, branchName, req.BaseVersion)
	if err != nil {
		if errors.Is(err, ErrServerRefuse) {
			return nil, fmt.Errorf("%w: maybe because branch `%s` already existed, retry one day later", err, branchName)
		}
		return nil, fmt.Errorf("create branch fail: %s", err.Error())
	}
	branchURL := GenBranchURL(repo.URL(), branchName)
	return &BranchCreateResp{Branch: branchName, BranchURL: branchURL}, nil
}

func (r repoService) CreateTag(ctx context.Context, req TagCreateReq) (resp *TagCreateResp, err error) {
	repo := prodToRepoMap[req.Prod]
	if repo == nil {
		return nil, fmt.Errorf("unknown product")
	}
	tagName := TagName(req.Branch, r.now().Format(FormatYYYYMMDD))
	if tagName == "" {
		return nil, fmt.Errorf("bad branch name, check if tag correct")
	}
	err = r.branchCreator.CreateTagFromBranch(ctx, *repo, tagName, req.Branch)
	if err != nil {
		return nil, err
	}
	tagURL := GenTagURL(repo.URL(), tagName)
	return &TagCreateResp{Tag: tagName, TagURL: tagURL}, nil
}

func TagName(branch string, date string) string {
	reg := regexp.MustCompile(`-v\d+.\d+.\d+$`)
	strWithMinus := reg.FindString(branch)
	if strWithMinus == "" {
		return ""
	}
	tag := strings.TrimPrefix(strWithMinus, "-")
	return fmt.Sprintf("%s-%s", tag, date)
}

func BranchName(version string, date string) string {
	twoNumVersion := GenTwoNumVersion(version)
	if twoNumVersion == "" {
		return ""
	}
	return fmt.Sprintf("release-%s-%s-%s", twoNumVersion, date, version)
}

func GenTwoNumVersion(version string) string {
	reg := regexp.MustCompile(`^v\d+\.\d+`)
	vstring := reg.FindString(version)
	if vstring == "" {
		return ""
	}
	return strings.TrimPrefix(vstring, "v")
}

func GenBranchURL(repoURL string, branchName string) string {
	return fmt.Sprintf("%s/tree/%s", repoURL, branchName)
}

func GenTagURL(repoURL string, tagName string) string {
	return fmt.Sprintf("%s/releases/tag/%s", repoURL, tagName)
}

func (r repoService) CreateTidbXHotfixTag(ctx context.Context, req TidbXHotfixTagCreateReq) (resp *TidbXHotfixTagCreateResp, err error) {
	// Validate that at least one of branch or commit is provided
	if req.Branch == "" && req.Commit == "" {
		return nil, fmt.Errorf("at least one of 'branch' or 'commit' must be provided")
	}

	// Parse the repo string
	repo := GHRepoToStruct(req.Repo)
	if repo == nil {
		return nil, fmt.Errorf("invalid repo format, expected 'owner/repo'")
	}

	var commitSHA string
	
	// If commit is provided, verify it exists
	if req.Commit != "" {
		commitSHA, err = r.branchCreator.GetCommitSHA(ctx, *repo, req.Commit)
		if err != nil {
			return nil, fmt.Errorf("failed to get commit SHA: %w", err)
		}
	}

	// If branch is provided, verify it exists and get the commit SHA if not already set
	if req.Branch != "" {
		branchCommitSHA, err := r.branchCreator.GetCommitSHA(ctx, *repo, req.Branch)
		if err != nil {
			return nil, fmt.Errorf("failed to get branch commit SHA: %w", err)
		}
		
		// If both branch and commit are provided, verify commit exists in the branch
		if req.Commit != "" {
			if commitSHA != branchCommitSHA {
				// Check if commit exists in branch's history
				branches, err := r.branchCreator.GetBranchesForCommit(ctx, *repo, commitSHA)
				if err != nil {
					return nil, fmt.Errorf("failed to verify commit in branch: %w", err)
				}
				found := false
				for _, b := range branches {
					if b == req.Branch {
						found = true
						break
					}
				}
				if !found {
					return nil, fmt.Errorf("commit %s does not exist in branch %s", req.Commit, req.Branch)
				}
			}
		} else {
			commitSHA = branchCommitSHA
		}
	}

	// Get all tags matching the pattern vX.Y.Z-nextgen.YYYYMM.N
	tags, err := r.branchCreator.ListTags(ctx, *repo)
	if err != nil {
		return nil, fmt.Errorf("failed to list tags: %w", err)
	}

	// Compute the new tag name
	newTag, err := ComputeNextgenTagName(tags, r.now())
	if err != nil {
		return nil, fmt.Errorf("failed to compute tag name: %w", err)
	}

	// Create the tag with author information in the message
	message := fmt.Sprintf("TiDB-X hotfix tag created by %s", req.Author)
	err = r.branchCreator.CreateAnnotatedTag(ctx, *repo, newTag, commitSHA, message)
	if err != nil {
		return nil, fmt.Errorf("failed to create tag: %w", err)
	}

	return &TidbXHotfixTagCreateResp{
		Repo:   req.Repo,
		Commit: commitSHA,
		Tag:    newTag,
	}, nil
}

// ComputeNextgenTagName computes the next tag name in the format vX.Y.Z-nextgen.YYYYMM.N
func ComputeNextgenTagName(tags []string, now time.Time) (string, error) {
	// Pattern: vX.Y.Z-nextgen.YYYYMM.N
	pattern := regexp.MustCompile(`^v(\d+)\.(\d+)\.(\d+)-nextgen\.(\d{6})\.(\d+)$`)
	
	currentYYYYMM := now.Format("200601") // YYYYMM format
	
	var maxN int = 0
	var baseVersion string = ""
	
	for _, tag := range tags {
		matches := pattern.FindStringSubmatch(tag)
		if matches != nil {
			tagYYYYMM := matches[4]
			if tagYYYYMM == currentYYYYMM {
				// Parse N
				var n int
				_, err := fmt.Sscanf(matches[5], "%d", &n)
				if err != nil {
					continue // Skip tags with invalid N component
				}
				if n > maxN {
					maxN = n
					baseVersion = fmt.Sprintf("v%s.%s.%s", matches[1], matches[2], matches[3])
				} else if baseVersion == "" {
					baseVersion = fmt.Sprintf("v%s.%s.%s", matches[1], matches[2], matches[3])
				}
			}
		}
	}
	
	// If no tags found for current month, start with version v8.5.4 (as per example) and N=1
	if baseVersion == "" {
		baseVersion = "v8.5.4"
		maxN = 0
	}
	
	newN := maxN + 1
	newTag := fmt.Sprintf("%s-nextgen.%s.%d", baseVersion, currentYYYYMM, newN)
	
	return newTag, nil
}

func NewRepoService(h RepoHeadCreator) HotfixService {
	return repoService{now: time.Now, branchCreator: h}
}

func NewRepoServiceForTest(h RepoHeadCreator, now func() time.Time) HotfixService {
	return repoService{now: now, branchCreator: h}
}
