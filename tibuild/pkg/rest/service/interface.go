package service

import "context"

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
}

type ArtifactHelperService interface {
	SyncImage(ctx context.Context, req ImageSyncRequest) (resp *ImageSyncRequest, err error)
}
