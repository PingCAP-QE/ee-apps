package impl

import (
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// transformDevBuild converts an ent.DevBuild to a devbuild.DevBuild
func transformDevBuild(build *ent.DevBuild) *devbuild.DevBuild {
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
			PluginGitRef:      &build.PluginGitRef,
			Product:           build.Product,
			ProductBaseImg:    &build.ProductBaseImg,
			ProductDockerfile: &build.ProductDockerfile,
			TargetImg:         &build.TargetImg,
			Version:           build.Version,
		},
		Status: &devbuild.DevBuildStatus{
			BuildReport:     transformBuildReport(build.BuildReport),
			ErrMsg:          &build.ErrMsg,
			PipelineBuildID: &build.PipelineBuildID,
			PipelineStartAt: ptr(build.PipelineStartAt.UTC().Format(time.DateTime)),
			PipelineEndAt:   ptr(build.PipelineEndAt.UTC().Format(time.DateTime)),
			Status:          devbuild.BuildStatus(build.Status),
			TektonStatus:    transformTektonStatus(build.TektonStatus),
		},
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
	// Add your transformation logic here.

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

// transformTektonStatus converts a map[string]any to a devbuild.TektonStatus
func transformTektonStatus(status map[string]any) *devbuild.TektonStatus {
	if status == nil {
		return nil
	}

	tektonStatus := &devbuild.TektonStatus{}

	// Add your transformation logic here.

	return tektonStatus
}

// Helper function to safely get string value from a map
func getString(m map[string]any, key string) string {
	if val, ok := m[key].(string); ok {
		return val
	}
	return ""
}

// ptr is a helper routine that allocates a new T value
// to store v and returns a pointer to it.
func ptr[T any](v T) *T {
	return &v
}
