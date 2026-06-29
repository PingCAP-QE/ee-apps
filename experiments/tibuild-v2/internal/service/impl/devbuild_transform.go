package impl

import (
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// transformDevBuild converts an ent.DevBuild to a devbuild.DevBuild
func transformDevBuild(build *ent.DevBuild) *devbuild.DevBuild {
	status := &devbuild.DevBuildStatus{
		BuildReport:     transformBuildReport(build.BuildReport),
		ErrMsg:          &build.ErrMsg,
		PipelineBuildID: &build.PipelineBuildID,
		PipelineStartAt: new(build.PipelineStartAt.UTC().Format(time.DateTime)),
		PipelineEndAt:   new(build.PipelineEndAt.UTC().Format(time.DateTime)),
		Status:          devbuild.BuildStatus(build.Status),
		TektonStatus:    transformTektonStatus(build.TektonStatus),
	}

	// Populate PipelineViewURLs from tekton pipeline URLs
	if build.TektonStatus.Pipelines != nil {
		var urls []string
		for _, p := range build.TektonStatus.Pipelines {
			if p.URL != "" {
				urls = append(urls, p.URL)
			}
		}
		if len(urls) > 0 {
			status.PipelineViewUrls = urls
		}
	}

	return &devbuild.DevBuild{
		ID: build.ID,
		Meta: &devbuild.DevBuildMeta{
			CreatedBy: build.CreatedBy,
			CreatedAt: build.CreatedAt.UTC().Format(time.DateTime),
			UpdatedAt: build.UpdatedAt.UTC().Format(time.DateTime),
		},
		Spec: &devbuild.DevBuildSpec{
			BuildEnv:          &build.BuildEnv,
			BuilderImg:        &build.BuilderImg,
			Edition:           build.Edition,
			Features:          &build.Features,
			GitSha:            &build.GitSha,
			GitRef:            build.GitRef,
			GithubRepo:        &build.GithubRepo,
			IsHotfix:          &build.IsHotfix,
			IsPushGcr:         &build.IsPushGcr,
			PipelineEngine:    &build.PipelineEngine,
			Platform:          build.Platform,
			PluginGitRef:      &build.PluginGitRef,
			Product:           build.Product,
			ProductBaseImg:    &build.ProductBaseImg,
			ProductDockerfile: &build.ProductDockerfile,
			TargetImg:         &build.TargetImg,
			Version:           build.Version,
		},
		Status: status,
	}
}

// transformBuildReport converts a map[string]any to a devbuild.BuildReport
func transformBuildReport(report map[string]any) *devbuild.BuildReport {
	if report == nil {
		return nil
	}

	buildReport := &devbuild.BuildReport{}

	if gitSha, ok := report["gitSha"].(string); ok {
		buildReport.GitSha = &gitSha
	}
	if pluginGitSha, ok := report["pluginGitSha"].(string); ok {
		buildReport.PluginGitSha = &pluginGitSha
	}
	if printedVersion, ok := report["printedVersion"].(string); ok {
		buildReport.PrintedVersion = &printedVersion
	}

	// Transform binaries
	if binariesRaw, ok := report["binaries"].([]any); ok {
		for _, binRaw := range binariesRaw {
			if oci, ok := binRaw.(map[string]any); ok {
				repo := getString(oci, "repo")
				tag := getString(oci, "tag")
				filesRaw, _ := oci["files"].([]any)
				var files []string
				for _, f := range filesRaw {
					if s, ok := f.(string); ok {
						files = append(files, s)
					}
				}
				binArtifacts := ociArtifactToBinArtifacts(repo, tag, files)
				buildReport.Binaries = append(buildReport.Binaries, binArtifacts...)
			}
		}
	}

	// Transform images
	if imagesRaw, ok := report["images"].([]any); ok {
		images := make([]*devbuild.ImageArtifact, 0, len(imagesRaw))
		for _, imgRaw := range imagesRaw {
			if img, ok := imgRaw.(map[string]any); ok {
				image := &devbuild.ImageArtifact{
					Platform: getString(img, "platform"),
					URL:      getString(img, "url"),
				}
				images = append(images, image)
			}
		}
		buildReport.Images = images
	}

	return buildReport
}

// transformTektonStatus converts a schema.TektonStatus to a devbuild.TektonStatus
func transformTektonStatus(status schema.TektonStatus) *devbuild.TektonStatus {
	tektonStatus := &devbuild.TektonStatus{
		TriggersEventIds: status.TriggersEventIds,
	}

	// Transform pipelines
	if len(status.Pipelines) > 0 {
		pipelines := make([]*devbuild.TektonPipeline, 0, len(status.Pipelines))
		for _, p := range status.Pipelines {
			pipeline := &devbuild.TektonPipeline{
				Name:      p.Name,
				Namespace: p.Namespace,
				Status:    devbuild.BuildStatus(p.Status),
				Platform:  &p.Platform,
				URL:       nonEmptyPtr(p.URL),
			}
			if p.StartAt != nil {
				pipeline.StartAt = new(p.StartAt.UTC().Format(time.RFC3339))
			}
			if p.EndAt != nil {
				pipeline.EndAt = new(p.EndAt.UTC().Format(time.RFC3339))
			}

			// Transform images
			if len(p.Images) > 0 {
				images := make([]*devbuild.ImageArtifact, 0, len(p.Images))
				for _, img := range p.Images {
					images = append(images, &devbuild.ImageArtifact{
						Platform: img.Platform,
						URL:      img.URL,
					})
				}
				pipeline.Images = images
			}

			// Transform OCI artifacts
			if len(p.OciArtifacts) > 0 {
				ociArtifacts := make([]*devbuild.OciArtifact, 0, len(p.OciArtifacts))
				for _, oci := range p.OciArtifacts {
					ociArtifacts = append(ociArtifacts, &devbuild.OciArtifact{
						Files: oci.Files,
						Repo:  oci.Repo,
						Tag:   oci.Tag,
					})
				}
				pipeline.OciArtifacts = ociArtifacts
			}

			pipelines = append(pipelines, pipeline)
		}
		tektonStatus.Pipelines = pipelines
	}

	return tektonStatus
}

// ociArtifactToBinArtifacts converts OCI artifact files to BinArtifact entries,
// pairing each binary with its .sha256 checksum file.
func ociArtifactToBinArtifacts(repo, tag string, files []string) []*devbuild.BinArtifact {
	var result []*devbuild.BinArtifact
	sha256Map := make(map[string]*devbuild.OciFile)

	for _, file := range files {
		if origin, ok := strings.CutSuffix(file, ".sha256"); ok {
			sha256Map[origin] = &devbuild.OciFile{Repo: repo, Tag: tag, File: file}
		} else {
			result = append(result, &devbuild.BinArtifact{
				OciFile: &devbuild.OciFile{Repo: repo, Tag: tag, File: file},
			})
		}
	}

	for _, bin := range result {
		if bin.OciFile != nil {
			bin.Sha256OciFile = sha256Map[bin.OciFile.File]
		}
	}

	return result
}

// Helper function to safely get string value from a map
func getString(m map[string]any, key string) string {
	if val, ok := m[key].(string); ok {
		return val
	}
	return ""
}

// nonEmptyPtr returns a pointer to v if v is non-empty, otherwise nil.
func nonEmptyPtr(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}
