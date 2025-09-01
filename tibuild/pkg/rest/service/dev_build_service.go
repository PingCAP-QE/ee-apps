package service

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/rs/zerolog/log"
)

type DevbuildServer struct {
	Repo             DevBuildRepository
	Jenkins          Jenkins
	Tekton           BuildTrigger
	Now              func() time.Time
	GHClient         GHClient
	TektonViewURL    string
	OciFileserverURL string
}

const devbuildJobname = "devbuild"

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
		err := fillDetailInfoForTekton(ctx, s.GHClient, &req)
		if err != nil {
			return nil, err
		}
		req.Status.TektonStatus = &TektonStatus{}
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
		qid, err := s.Jenkins.BuildJob(ctx, devbuildJobname, params)
		if err != nil {
			entity.Status.Status = BuildStatusError
			entity.Status.ErrMsg = err.Error()
			s.Repo.Update(ctx, entity.ID, entity)
			return nil, fmt.Errorf("trigger jenkins fail: %w", ErrInternalError)
		}
		go func(entity DevBuild) {
			buildNumber, err := s.Jenkins.GetBuildNumberFromQueueID(ctx, qid)
			if err != nil {
				entity.Status.Status = BuildStatusError
				entity.Status.ErrMsg = err.Error()
				s.Repo.Update(ctx, entity.ID, entity)
			}
			log.Info().Int64("build_number", buildNumber).Msg("Jenkins build number got")
			entity.Status.PipelineBuildID = buildNumber
			entity.Status.Status = BuildStatusProcessing
			s.Repo.Update(ctx, entity.ID, entity)
		}(entity)
	} else if entity.Spec.PipelineEngine == TektonEngine {
		err = s.Tekton.TriggerDevBuild(ctx, entity)
		if err != nil {
			log.Error().Err(err).Msg("trigger tekton failed")
			entity.Status.Status = BuildStatusError
			entity.Status.ErrMsg = err.Error()
			_, err := s.Repo.Update(ctx, entity.ID, entity)
			if err != nil {
				log.Error().Err(err).Msg("save triggered entity failed")
			}
			return nil, fmt.Errorf("trigger Tekton fail: %w", ErrInternalError)
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

func fillDetailInfoForTekton(ctx context.Context, client GHClient, req *DevBuild) error {
	repo := GHRepoToStruct(req.Spec.GithubRepo)
	switch {
	case strings.HasPrefix(req.Spec.GitRef, "pull/"):
		prNumber, err := strconv.ParseInt(strings.Replace(req.Spec.GitRef, "pull/", "", 1), 10, 32)
		if err != nil {
			return err
		}
		req.Spec.prNumber = int(prNumber)

		pr, err := client.GetPullRequestInfo(ctx, repo.Owner, repo.Repo, req.Spec.prNumber)
		if err != nil {
			return err
		}
		req.Spec.GitHash = pr.Head.GetSHA()
		req.Spec.prBaseRef = pr.Base.GetRef()

		return nil
	case strings.HasPrefix(req.Spec.GitRef, "tag/"),
		strings.HasPrefix(req.Spec.GitRef, "branch/"):
		commit, err := client.GetHash(ctx, repo.Owner, repo.Repo, req.Spec.GitRef)
		if err != nil {
			return fmt.Errorf("get hash from github failed%s%w", err.Error(), ErrServerRefuse)
		}
		req.Spec.GitHash = commit

		return nil
	case strings.HasPrefix(req.Spec.GitRef, "commit/") || regexp.MustCompile(`^[a-fA-F\d]+$`).MatchString(req.Spec.GitRef):
		commit := strings.Replace(req.Spec.GitRef, "commit/", "", 1)
		req.Spec.GitHash = commit
		branch, err := getBranchForCommit(ctx, client, repo.Owner, repo.Repo, commit)
		if err != nil {
			return err
		}

		req.Spec.GitRef = "branch/" + branch
		return nil
	default:
		return nil
	}
}

// get the branch name for the commit in github <owner>/<repo> repo.
func getBranchForCommit(ctx context.Context, client GHClient, owner, repo, commit string) (string, error) {
	branches, err := client.GetBranchesForCommit(ctx, owner, repo, commit)
	if err != nil {
		return "", err
	}
	if len(branches) == 0 {
		return "", fmt.Errorf("no branch found for commit %s", commit)
	}

	return branches[0], nil
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
	if spec.Product == ProductTidb && spec.Edition == EditionEnterprise && spec.PluginGitRef == "" {
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
		repo := prodToRepoMap[spec.Product]
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
		if spec.Product == ProductTidb && spec.Edition == EditionEnterprise {
			fileName = fileName + "-enterprise"
		}
		dockerfile := fmt.Sprintf("https://raw.githubusercontent.com/PingCAP-QE/artifacts/main/dockerfiles/%s.Dockerfile", fileName)
		spec.ProductDockerfile = dockerfile
	}
}

func hasFIPS(feature string) bool {
	features := strings.Split(feature, " ")
	return slices.Contains(features, FIPS_FEATURE)
}

func validateReq(req DevBuild) error {
	spec := req.Spec
	if !slices.Contains(allProducts, spec.Product) {
		return fmt.Errorf("product %s is invalid, valid list is: %s", spec.Product, strings.Join(allProducts, ","))
	}

	// validate for edition for different pipeline engines
	switch spec.PipelineEngine {
	case JenkinsEngine:
		if !slices.Contains(InvalidEditionForJenkins, spec.Edition) {
			return fmt.Errorf("edition is not valid for jenkins engine")
		}
	case TektonEngine:
		if !slices.Contains(InvalidEditionForTekton, spec.Edition) {
			return fmt.Errorf("edition is not valid for tekton engine")
		}
	default:
		return fmt.Errorf("pipeline engine is not valid")
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
	if spec.Edition == EditionEnterprise && spec.Product == ProductTidb {
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
	if spec.PipelineEngine == JenkinsEngine && spec.Platform != "" {
		return errors.New("cannot set platform when pipeline engine is Jenkins")
	}
	if spec.PipelineEngine == JenkinsEngine && !slices.Contains(supportedProductsInJenkinsEngine, spec.Product) {
		return fmt.Errorf("product %s is not supported by Jenkins engine implementation!", spec.Product)
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
	if !IsValidBuildStatus(req.Status.Status) {
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
	build, err := s.Jenkins.GetBuild(ctx, devbuildJobname, entity.Status.PipelineBuildID)
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
	if IsBuildStatusCompleted(entity.Status.Status) && entity.Status.PipelineEndAt == nil {
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
		entity.Status.PipelineViewURL = s.Jenkins.BuildURL(devbuildJobname, entity.Status.PipelineBuildID)
		entity.Status.PipelineViewURLs = append(entity.Status.PipelineViewURLs, entity.Status.PipelineViewURL)
	}
	if entity.Status.BuildReport != nil {
		for i, bin := range entity.Status.BuildReport.Binaries {
			if bin.URL == "" && bin.OciFile != nil {
				entity.Status.BuildReport.Binaries[i].URL = s.ociFileToUrl(*bin.OciFile)
			}
			if bin.Sha256OciFile != nil {
				entity.Status.BuildReport.Binaries[i].Sha256URL = s.ociFileToUrl(*bin.Sha256OciFile)
			}
		}
	}
	if tek := entity.Status.TektonStatus; tek != nil {
		for _, p := range tek.Pipelines {
			entity.Status.PipelineViewURLs = append(entity.Status.PipelineViewURLs, fmt.Sprintf("%s/%s", s.TektonViewURL, p.Name))
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
	tekton := obj.Status.TektonStatus
	name := pipeline.Name
	index := -1
	for i, p := range tekton.Pipelines {
		if p.Name == name {
			index = i
		}
	}
	if index >= 0 {
		tekton.Pipelines[index] = pipeline
	} else {
		tekton.Pipelines = append(tekton.Pipelines, pipeline)
	}
	if obj.Spec.PipelineEngine == TektonEngine {
		computeTektonStatus(tekton, &obj.Status)
	}
	return s.Update(ctx, id, *obj, options)
}

func computeTektonStatus(tekton *TektonStatus, status *DevBuildStatus) {
	status.BuildReport = &BuildReport{}
	collectTektonArtifacts(tekton.Pipelines, status.BuildReport)
	status.PipelineStartAt = getTektonStartAt(tekton.Pipelines)
	status.Status = computeTektonPhase(tekton.Pipelines)
	if IsBuildStatusCompleted(status.Status) {
		status.PipelineEndAt = getLatestEndAt(tekton.Pipelines)
	}
}

func collectTektonArtifacts(pipelines []TektonPipeline, report *BuildReport) {
	for _, pipeline := range pipelines {
		report.GitHash = pipeline.GitHash
		for _, files := range pipeline.OciArtifacts {
			report.Binaries = append(report.Binaries, ociArtifactToFiles(pipeline.Platform, files)...)
		}
		for _, image := range pipeline.Images {
			ri := image
			if ri.Platform == "" {
				ri.Platform = pipeline.Platform
			}
			report.Images = append(report.Images, ri)
		}
	}
}

func getTektonStartAt(pipelines []TektonPipeline) *time.Time {
	var startAt *time.Time = nil
	for _, pipeline := range pipelines {
		if pipeline.StartAt != nil {
			if startAt == nil {
				startAt = pipeline.StartAt
			} else if pipeline.StartAt.Before(*startAt) {
				startAt = pipeline.StartAt
			}
		}
	}
	return startAt
}

func computeTektonPhase(pipelines []TektonPipeline) string {
	phase := BuildStatusPending
	var success_platforms = map[string]struct{}{}
	var failure_platforms = map[string]struct{}{}
	var triggered_platforms = map[string]struct{}{}
	for _, pipeline := range pipelines {
		switch pipeline.Status {
		case BuildStatusSuccess:
			success_platforms[pipeline.Platform] = struct{}{}
		case BuildStatusFailure:
			failure_platforms[pipeline.Platform] = struct{}{}
		}
		triggered_platforms[pipeline.Platform] = struct{}{}
	}
	if len(success_platforms) == len(triggered_platforms) {
		phase = BuildStatusSuccess
	} else if len(failure_platforms) != 0 {
		phase = BuildStatusFailure
	} else if len(pipelines) != 0 {
		phase = BuildStatusProcessing
	}
	return phase
}

func getLatestEndAt(pipelines []TektonPipeline) *time.Time {
	var latest_endat *time.Time
	for _, pipeline := range pipelines {
		if pipeline.EndAt != nil {
			if latest_endat == nil {
				latest_endat = pipeline.EndAt
			} else if latest_endat.Before(*pipeline.EndAt) {
				latest_endat = pipeline.EndAt
			}
		}
	}
	return latest_endat
}

func ociArtifactToFiles(platform string, artifact OciArtifact) []BinArtifact {
	var rt []BinArtifact
	var sha256s = make(map[string]*OciFile)
	for _, file := range artifact.Files {
		if origin, isSha256 := strings.CutSuffix(file, ".sha256"); isSha256 {
			sha256s[origin] = &OciFile{Repo: artifact.Repo, Tag: artifact.Tag, File: file}
		} else {
			rt = append(rt, BinArtifact{Platform: platform, OciFile: &OciFile{Repo: artifact.Repo, Tag: artifact.Tag, File: file}})
		}
	}
	for idx := 0; idx < len(rt); idx += 1 {
		rt[idx].Sha256OciFile = sha256s[rt[idx].OciFile.File]
	}
	return rt
}

func (s DevbuildServer) ociFileToUrl(artifact OciFile) string {
	return fmt.Sprintf("%s/%s?tag=%s&file=%s", s.OciFileserverURL, artifact.Repo, artifact.Tag, artifact.File)
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
var gitRefValidator *regexp.Regexp = regexp.MustCompile(`^((master|main|release-.*|v\d.*|[0-9a-fA-F]{40})|(tag/.+)|(branch/.+)|(pull/\d+)|(commit/[0-9a-fA-F]{40}))$`)
var githubRepoValidator *regexp.Regexp = regexp.MustCompile(`^([\w_-]+/[\w_-]+)$`)
