package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"regexp"
	"time"
)

type DevbuildServer struct {
	Repo    DevBuildRepository
	Jenkins Jenkins
	Now     func() time.Time
}

const jobname = "devbuild"

func (s DevbuildServer) Create(ctx context.Context, req DevBuild, option DevBuildSaveOption) (*DevBuild, error) {
	req.Status = DevBuildStatus{}
	req.Meta.CreatedAt = s.Now()
	req.Status.Status = BuildStatusPending

	if err := validate_permission(ctx, req); err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrAuth)
	}

	fillWithDefaults(&req)
	if err := validateReq(req); err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrBadRequest)
	}
	if option.DryRun {
		req.ID = 1
		return &req, nil
	}
	resp, err := s.Repo.Create(ctx, req)
	if err != nil {
		return nil, err
	}
	entity := *resp

	params := map[string]string{
		"Product":           string(entity.Spec.Product),
		"GitRef":            entity.Spec.GitRef,
		"Version":           entity.Spec.Version,
		"Edition":           string(entity.Spec.Edition),
		"PluginGitRef":      entity.Spec.PluginGitRef,
		"GithubRepo":        entity.Spec.GithubRepo,
		"IsPushGCR":         strconv.FormatBool(entity.Spec.IsPushGCR),
		"IsHotfix":          strconv.FormatBool(entity.Spec.IsHotfix),
		"Features":          entity.Spec.Features,
		"TiBuildID":         strconv.Itoa(entity.ID),
		"BuildEnv":          entity.Spec.BuildEnv,
		"BuilderImg":        entity.Spec.BuilderImg,
		"ProductDockerfile": entity.Spec.ProductDockerfile,
		"ProductBaseImg":    entity.Spec.ProductBaseImg,
	}
	qid, err := s.Jenkins.BuildJob(ctx, jobname, params)
	if err != nil {
		entity.Status.Status = BuildStatusError
		entity.Status.ErrMsg = err.Error()
		s.Repo.Update(ctx, entity.ID, entity)
		return nil, fmt.Errorf("trigger jenkins fail: %w", ErrInternalError)
	}
	go func(entity DevBuild) {
		buildNumber, err := s.Jenkins.GetBuildNumberFromQueueID(ctx, qid, jobname)
		if err != nil {
			entity.Status.Status = BuildStatusError
			entity.Status.ErrMsg = err.Error()
			s.Repo.Update(ctx, entity.ID, entity)
		}
		println("Jenkins build number is : ", buildNumber)
		entity.Status.PipelineBuildID = buildNumber
		entity.Status.Status = BuildStatusProcessing
		s.Repo.Update(ctx, entity.ID, entity)
	}(entity)
	return &entity, nil
}

func validate_permission(ctx context.Context, req DevBuild) error {
	if req.Spec.TargetImage != "" && ctx.Value(KeyOfUserName) != AdminUserName {
		return fmt.Errorf("targetImage deny because of permission")
	}
	return nil
}

func fillWithDefaults(req *DevBuild) {
	spec := &req.Spec
	guessEnterprisePluginRef(spec)
	fillGithubRepo(spec)
	fillForFIPS(spec)
	req.Status.BuildReportJson = json.RawMessage("null")
}

func guessEnterprisePluginRef(spec *DevBuildSpec) {
	if spec.Product == ProductTidb && spec.Edition == EnterpriseEdition && spec.PluginGitRef == "" {
		groups := versionValidator.FindStringSubmatch(spec.Version)
		if len(groups) == 2 {
			major := groups[1]
			spec.PluginGitRef = fmt.Sprintf("release-%s", major)
		}
	}
}

func fillGithubRepo(spec *DevBuildSpec) {
	if spec.GithubRepo == "" {
		repo := ProdToRepo(spec.Product)
		if repo != nil {
			spec.GithubRepo = fmt.Sprintf("%s/%s", repo.Owner, repo.Repo)
		}
	}
}

const FIPS_FEATURE = "fips"
const FIPS_BUILD_ENV = "ENABLE_FIPS=1"
const FIPS_TIKV_BUILDER = "hub.pingcap.net/jenkins/tikv-builder:fips"
const FIPS_TIKV_BASE = "hub.pingcap.net/bases/tikv-base:v1-fips"

func fillForFIPS(spec *DevBuildSpec) {
	if !hasFIPS(spec.Features) {
		return
	}
	if !strings.Contains(spec.BuildEnv, FIPS_BUILD_ENV) {
		if spec.BuildEnv != "" {
			spec.BuildEnv = FIPS_BUILD_ENV + " " + spec.BuildEnv
		} else {
			spec.BuildEnv = FIPS_BUILD_ENV
		}
	}

	if spec.Product == ProductTikv {
		if spec.BuilderImg == "" {
			spec.BuilderImg = FIPS_TIKV_BUILDER
		}
		if spec.ProductBaseImg == "" {
			spec.ProductBaseImg = FIPS_TIKV_BASE
		}
	} else {
		fileName := spec.Product
		if spec.Product == ProductTidb && spec.Edition == EnterpriseEdition {
			fileName = fileName + "-enterprise"
		}
		dockerfile := fmt.Sprintf("https://raw.githubusercontent.com/PingCAP-QE/artifacts/main/dockerfiles/%s.Dockerfile", fileName)
		spec.ProductDockerfile = dockerfile
	}
}

func hasFIPS(feature string) bool {
	features := strings.Split(feature, " ")
	for _, f := range features {
		if f == FIPS_FEATURE {
			return true
		}
	}
	return false

}

func validateReq(req DevBuild) error {
	spec := req.Spec
	if !spec.Product.IsValid() {
		return fmt.Errorf("product is not valid")
	}
	if !spec.Edition.IsValid() {
		return fmt.Errorf("edition is not valid")
	}
	if !versionValidator.MatchString(spec.Version) {
		return fmt.Errorf("version is not valid")
	}
	if !gitRefValidator.MatchString(spec.GitRef) {
		return fmt.Errorf("gitRef is not valid")
	}
	if spec.GithubRepo != "" && !githubRepoValidator.MatchString(spec.GithubRepo) {
		return fmt.Errorf("githubRepo is not valid, should be like org/repo")
	}
	if spec.Edition == EnterpriseEdition && spec.Product == ProductTidb {
		if !gitRefValidator.MatchString(spec.PluginGitRef) {
			return fmt.Errorf("pluginGitRef is not valid")
		}
	}
	if spec.IsHotfix {
		if !hotfixVersionValidator.MatchString((spec.Version)) {
			return fmt.Errorf("verion must be like v7.0.0-20230102... for hotfix")
		}
	}
	return nil
}

func (s DevbuildServer) Update(ctx context.Context, id int, req DevBuild, option DevBuildSaveOption) (resp *DevBuild, err error) {
	if id == 0 || req.ID != 0 && req.ID != id {
		return nil, fmt.Errorf("bad id%w", ErrBadRequest)
	}
	req.ID = id
	old, err := s.Repo.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	if !req.Status.Status.IsValid() {
		return nil, fmt.Errorf("bad status%w", ErrBadRequest)
	}
	old.Status = req.Status
	old.Meta.UpdatedAt = s.Now()
	if option.DryRun {
		return old, nil
	}
	return s.Repo.Update(ctx, id, *old)
}

func (s DevbuildServer) List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error) {
	return s.Repo.List(ctx, option)
}
func (s DevbuildServer) Rerun(ctx context.Context, id int, option DevBuildSaveOption) (*DevBuild, error) {
	old, err := s.Get(ctx, id, DevBuildGetOption{})
	if err != nil {
		return nil, err
	}
	obj := DevBuild{}
	obj.Spec = old.Spec
	return s.Create(ctx, obj, option)
}

func (s DevbuildServer) Get(ctx context.Context, id int, option DevBuildGetOption) (*DevBuild, error) {
	entity, err := s.Repo.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	if option.Sync && entity.Status.Status == BuildStatusProcessing {
		entity, err = s.sync(ctx, entity)
		if err != nil {
			return nil, err
		}
		check, err := s.Repo.Get(ctx, id)
		if err != nil {
			return nil, err
		}
		if check.Meta.UpdatedAt != entity.Meta.UpdatedAt {
			return nil, fmt.Errorf("update failed because of race condition%w", ErrInternalError)
		}
		entity.Meta.UpdatedAt = s.Now()
		entity, err = s.Repo.Update(ctx, id, *entity)
		if err != nil {
			return nil, err
		}
	}
	s.inflate(entity)
	return entity, nil
}

func (s DevbuildServer) sync(ctx context.Context, entity *DevBuild) (*DevBuild, error) {
	now := s.Now()
	build, err := s.Jenkins.GetBuild(ctx, jobname, entity.Status.PipelineBuildID)
	if err != nil {
		return nil, fmt.Errorf("fetch jenkins status error%w", ErrInternalError)
	}
	switch build.GetResult() {
	case "SUCCESS":
		entity.Status.Status = BuildStatusSuccess
	case "FAILURE":
		entity.Status.Status = BuildStatusFailure
	case "ABORTED":
		entity.Status.Status = BuildStatusAborted
	}
	startAt := build.GetTimestamp().Local()
	entity.Status.PipelineStartAt = &startAt
	if entity.Status.Status.IsCompleted() && entity.Status.PipelineEndAt == nil {
		if build.GetDuration() != 0 {
			endAt := startAt.Add(time.Duration(build.GetDuration() * float64(time.Microsecond)))
			entity.Status.PipelineEndAt = &endAt
		} else {
			entity.Status.PipelineEndAt = &now
		}
	}
	return entity, nil
}

func (s DevbuildServer) inflate(entity *DevBuild) {
	if entity.Status.PipelineBuildID != 0 {
		entity.Status.PipelineViewURL = s.Jenkins.BuildURL(jobname, entity.Status.PipelineBuildID)
	}
}

type DevBuildRepository interface {
	Create(ctx context.Context, req DevBuild) (resp *DevBuild, err error)
	Get(ctx context.Context, id int) (resp *DevBuild, err error)
	Update(ctx context.Context, id int, req DevBuild) (resp *DevBuild, err error)
	List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error)
}

var _ DevBuildService = DevbuildServer{}

var versionValidator *regexp.Regexp
var hotfixVersionValidator *regexp.Regexp
var gitRefValidator *regexp.Regexp
var githubRepoValidator *regexp.Regexp

func init() {
	versionValidator = regexp.MustCompile(`^v(\d+\.\d+)\.\d+.*$`)
	hotfixVersionValidator = regexp.MustCompile(`^v(\d+\.\d+)\.\d+-\d{8,}.*$`)
	gitRefValidator = regexp.MustCompile(`^((v\d.*)|(pull/\d+)|([0-9a-fA-F]{40})|(release-.*)|master|main|(tag/.+)|(branch/.+))$`)
	githubRepoValidator = regexp.MustCompile(`^([\w_-]+/[\w_-]+)$`)
}
