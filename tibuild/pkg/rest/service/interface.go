package service

import (
	"context"

	"github.com/google/go-github/v61/github"
)

type HotfixService interface {
	CreateBranch(ctx context.Context, req BranchCreateReq) (resp *BranchCreateResp, err error)
	CreateTag(ctx context.Context, req TagCreateReq) (resp *TagCreateResp, err error)
}

type DevBuildService interface {
	Create(ctx context.Context, req DevBuild, option DevBuildSaveOption) (resp *DevBuild, err error)
	Get(ctx context.Context, id int, option DevBuildGetOption) (resp *DevBuild, err error)
	Rerun(ctx context.Context, id int, option DevBuildSaveOption) (resp *DevBuild, err error)
	Update(ctx context.Context, id int, req DevBuild, option DevBuildSaveOption) (resp *DevBuild, err error)
	List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error)
	MergeTektonStatus(ctx context.Context, id int, pipeline TektonPipeline, options DevBuildSaveOption) (resp *DevBuild, err error)
}

type ArtifactHelperService interface {
	SyncImage(ctx context.Context, req ImageSyncRequest) (resp *ImageSyncRequest, err error)
}

type GHClient interface {
	GetHash(ctx context.Context, owner, repo, ref string) (string, error)
	GetPullRequestInfo(ctx context.Context, owner, repo string, prNum int) (*github.PullRequest, error)
}
