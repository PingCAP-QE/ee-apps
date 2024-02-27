package service

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"

	"regexp"
	"time"
)

type DevbuildServer struct {
	Repo     DevBuildRepository
	Jenkins  Jenkins
	Tekton   BuildTrigger
	Now      func() time.Time
	GHClient GHClient
}

const jobname = "devbuild"

func (s DevbuildServer) Create(ctx context.Context, req DevBuild, option DevBuildSaveOption) (*DevBuild, error) {
	req.Status = DevBuildStatus{}
	req.Meta.CreatedAt = s.Now()
	req.Status.Status = BuildStatusPending

	if err := validatePermission(ctx, &req); err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrAuth)
	}

	fillWithDefaults(&req)
	if err := validateReq(req); err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrBadRequest)
	}
	if req.Spec.PipelineEngine == TektonEngine {
		if req.Meta.CreatedBy == "" {
			return nil, fmt.Errorf("unkown submitter%w", ErrAuth)
		}
		err := fillGitHash(ctx, s.GHClient, &req)
		if err != nil {
			return nil, err
		}
		req.Status.TektonStatus = &TektonStatus{Status: BuildStatusPending}
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
	if entity.Spec.PipelineEngine == JenkinsEngine {
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
			"TargetImg":         entity.Spec.TargetImg,
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
	} else if entity.Spec.PipelineEngine == TektonEngine {
		err = s.Tekton.TriggerDevBuild(ctx, entity)
		if err != nil {
			slog.Error("trigger tekton failed", "reason", err)
			entity.Status.Status = BuildStatusError
			entity.Status.ErrMsg = err.Error()
			_, err := s.Repo.Update(ctx, entity.ID, entity)
			if err != nil {
				slog.Error("save triggered entity failed", "reason", err)
			}
			return nil, fmt.Errorf("trigger jenkins fail: %w", ErrInternalError)
		}
	}

	return &entity, nil
}

func validatePermission(ctx context.Context, req *DevBuild) error {
	if req.Spec.TargetImg != "" && ctx.Value(KeyOfApiAccount) != AdminApiAccount {
		return fmt.Errorf("targetImage deny because of permission")
	}
	return nil
}

func fillGitHash(ctx context.Context, client GHClient, req *DevBuild) error {
	if req.Spec.GitHash != "" {
		return nil
	}
	commit, err := client.GetHash(ctx, *GHRepoToStruct(req.Spec.GithubRepo), req.Spec.GitRef)
	if err != nil {
		return fmt.Errorf("get hash from github failed%s%w", err.Error(), ErrServerRefuse)
	}
	req.Spec.GitHash = commit
	return nil
}

func fillWithDefaults(req *DevBuild) {
	spec := &req.Spec
	guessEnterprisePluginRef(spec)
	fillGithubRepo(spec)
	fillForFIPS(spec)
	if req.Spec.PipelineEngine == "" {
		req.Spec.PipelineEngine = JenkinsEngine
	}
}

func guessEnterprisePluginRef(spec *DevBuildSpec) {
	if spec.Product == ProductTidb && spec.Edition == EnterpriseEdition && spec.PluginGitRef == "" {
		groups := versionValidator.FindStringSubmatch(spec.Version)
		if len(groups) == 3 {
			major_sub := groups[1]
			if spec.IsHotfix {
				patch := groups[2]
				spec.PluginGitRef = fmt.Sprintf("release-%s%s", major_sub, patch)
			} else {
				spec.PluginGitRef = fmt.Sprintf("release-%s", major_sub)
			}
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
	if spec.GithubRepo != "" && (GHRepoToStruct(spec.GithubRepo) == nil || !githubRepoValidator.MatchString(spec.GithubRepo)) {
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
		if spec.TargetImg != "" {
			return fmt.Errorf("target image shall be empty for hotfix")
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
	if req.Status.TektonStatus == nil {
		req.Status.TektonStatus = old.Status.TektonStatus
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
	if entity.Spec.PipelineEngine == TektonEngine {
		return entity, nil
	}
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
	if entity.Status.BuildReport != nil {
		for i, bin := range entity.Status.BuildReport.Binaries {
			if bin.URL == "" && bin.OrasFile != nil {
				entity.Status.BuildReport.Binaries[i].URL = oras_to_file_url(*bin.OrasFile)
			}
		}
	}
	if tek := entity.Status.TektonStatus; tek != nil {
		for i, p := range tek.Pipelines {
			tek.Pipelines[i].URL = fmt.Sprintf("%s/%s", tektonURL, p.Name)
		}
	}
}

func (s DevbuildServer) MergeTektonStatus(ctx context.Context, id int, pipeline TektonPipeline, options DevBuildSaveOption) (resp *DevBuild, err error) {
	obj, err := s.Get(ctx, id, DevBuildGetOption{})
	if err != nil {
		return nil, err
	}
	if obj.Status.TektonStatus == nil {
		obj.Status.TektonStatus = &TektonStatus{}
	}
	status := obj.Status.TektonStatus
	name := pipeline.Name
	index := -1
	for i, p := range status.Pipelines {
		if p.Name == name {
			index = i
		}
	}
	if index >= 0 {
		status.Pipelines[index] = pipeline
	} else {
		status.Pipelines = append(status.Pipelines, pipeline)
	}
	compute_tekton_status(status)
	if obj.Spec.PipelineEngine == TektonEngine {
		obj.Status.Status = obj.Status.TektonStatus.Status
		obj.Status.BuildReport = obj.Status.TektonStatus.BuildReport
	}
	return s.Update(ctx, id, *obj, options)
}

func compute_tekton_status(status *TektonStatus) {
	phase := BuildStatusPending
	var success_platforms = map[Platform]struct{}{}
	var failure_platforms = map[Platform]struct{}{}
	var triggered_platforms = map[Platform]struct{}{}
	var latest_endat *time.Time
	if status.BuildReport == nil {
		status.BuildReport = &BuildReport{}
	} else {
		status.BuildReport.Images = nil
		status.BuildReport.Binaries = nil
	}
	for _, pipeline := range status.Pipelines {
		switch pipeline.Status {
		case BuildStatusSuccess:
			success_platforms[pipeline.Platform] = struct{}{}
		case BuildStatusFailure:
			failure_platforms[pipeline.Platform] = struct{}{}
		}
		triggered_platforms[pipeline.Platform] = struct{}{}
		status.BuildReport.GitHash = pipeline.GitHash
		for _, files := range pipeline.OrasArtifacts {
			status.BuildReport.Binaries = append(status.BuildReport.Binaries, oras_to_files(pipeline.Platform, files)...)
		}
		for _, image := range pipeline.Images {
			img := ImageArtifact{Platform: pipeline.Platform, URL: image.URL}
			status.BuildReport.Images = append(status.BuildReport.Images, img)
		}
		if pipeline.PipelineStartAt != nil {
			if status.PipelineStartAt == nil {
				status.PipelineStartAt = pipeline.PipelineStartAt
			} else if pipeline.PipelineStartAt.Before(*status.PipelineStartAt) {
				status.PipelineStartAt = pipeline.PipelineStartAt
			}
		}
		if pipeline.PipelineEndAt != nil {
			if latest_endat == nil {
				latest_endat = pipeline.PipelineEndAt
			} else if latest_endat.Before(*pipeline.PipelineEndAt) {
				latest_endat = pipeline.PipelineEndAt
			}
		}
	}
	if len(success_platforms) == len(triggered_platforms) {
		phase = BuildStatusSuccess
	} else if len(failure_platforms) != 0 {
		phase = BuildStatusFailure
	} else if len(status.Pipelines) != 0 {
		phase = BuildStatusProcessing
	}
	status.Status = phase
	if status.Status.IsCompleted() {
		status.PipelineEndAt = latest_endat
	}
}

func oras_to_files(platform Platform, oras OrasArtifact) []BinArtifact {
	var rt []BinArtifact
	for _, file := range oras.Files {
		rt = append(rt, BinArtifact{Platform: platform, OrasFile: &OrasFile{Repo: oras.Repo, Tag: oras.Tag, File: file}})
	}
	return rt
}

func oras_to_file_url(oras OrasFile) string {
	return fmt.Sprintf("%s/oci-file/%s?tag=%s&file=%s", oras_fileserver_url, oras.Repo, oras.Tag, oras.File)
}

type DevBuildRepository interface {
	Create(ctx context.Context, req DevBuild) (resp *DevBuild, err error)
	Get(ctx context.Context, id int) (resp *DevBuild, err error)
	Update(ctx context.Context, id int, req DevBuild) (resp *DevBuild, err error)
	List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error)
}

var _ DevBuildService = DevbuildServer{}

var versionValidator *regexp.Regexp = regexp.MustCompile(`^v(\d+\.\d+)(\.\d+).*$`)
var hotfixVersionValidator *regexp.Regexp = regexp.MustCompile(`^v(\d+\.\d+)\.\d+-\d{8,}.*$`)
var gitRefValidator *regexp.Regexp = regexp.MustCompile(`^((v\d.*)|(pull/\d+)|([0-9a-fA-F]{40})|(release-.*)|master|main|(tag/.+)|(branch/.+))$`)
var githubRepoValidator *regexp.Regexp = regexp.MustCompile(`^([\w_-]+/[\w_-]+)$`)

const tektonURL = "https://do.pingcap.net/tekton/#/namespaces/ee-cd/pipelineruns"
const oras_fileserver_url = "https://internal.do.pingcap.net:30443/dl"
