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

func NewRepoService(h RepoHeadCreator) HotfixService {
	return repoService{now: time.Now, branchCreator: h}
}

func NewRepoServiceForTest(h RepoHeadCreator, now func() time.Time) HotfixService {
	return repoService{now: now, branchCreator: h}
}
