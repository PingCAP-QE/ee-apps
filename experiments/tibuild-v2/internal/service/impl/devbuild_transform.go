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
			BuildEnv:          build.BuildEnv,
			BuilderImg:        build.BuilderImg,
			Edition:           devbuild.ProductEdition(build.Edition),
			Features:          build.Features,
			GitHash:           build.GitHash,
			GitRef:            build.GitRef,
			GithubRepo:        build.GithubRepo,
			IsHotfix:          build.IsHotfix,
			IsPushGcr:         build.IsPushGcr,
			PipelineEngine:    devbuild.PipelineEngine(build.PipelineEngine),
			PluginGitRef:      build.PluginGitRef,
			Product:           devbuild.Product(build.Product),
			ProductBaseImg:    build.ProductBaseImg,
			ProductDockerfile: build.ProductDockerfile,
			TargetImg:         build.TargetImg,
			Version:           build.Version,
		},
		Status: &devbuild.DevBuildStatus{
			BuildReport:     transformBuildReport(build.BuildReport),
			ErrMsg:          build.ErrMsg,
			PipelineBuildID: int(build.PipelineBuildID),
			PipelineStartAt: build.PipelineStartAt.UTC().Format(time.DateTime),
			PipelineEndAt:   build.PipelineEndAt.UTC().Format(time.DateTime),
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

	if gitHash, ok := report["gitHash"].(string); ok {
		buildReport.GitHash = gitHash
	}
	if pluginGitHash, ok := report["pluginGitHash"].(string); ok {
		buildReport.PluginGitHash = pluginGitHash
	}
	if printedVersion, ok := report["printedVersion"].(string); ok {
		buildReport.PrintedVersion = printedVersion
	}

	// Transform binaries
	if binariesRaw, ok := report["binaries"].([]any); ok {
		binaries := make([]*devbuild.BinArtifact, 0, len(binariesRaw))
		for _, binRaw := range binariesRaw {
			if bin, ok := binRaw.(map[string]any); ok {
				artifact := &devbuild.BinArtifact{}

				if component, ok := bin["component"].(string); ok {
					artifact.Component = component
				}
				if platform, ok := bin["platform"].(string); ok {
					artifact.Platform = platform
				}
				if url, ok := bin["url"].(string); ok {
					artifact.URL = url
				}
				if sha256URL, ok := bin["sha256URL"].(string); ok {
					artifact.Sha256URL = sha256URL
				}

				// Transform OciFile
				if ociFileRaw, ok := bin["ociFile"].(map[string]any); ok {
					artifact.OciFile = &devbuild.OciFile{
						File: getString(ociFileRaw, "file"),
						Repo: getString(ociFileRaw, "repo"),
						Tag:  getString(ociFileRaw, "tag"),
					}
				}

				// Transform Sha256OciFile
				if sha256OciFileRaw, ok := bin["sha256OciFile"].(map[string]any); ok {
					artifact.Sha256OciFile = &devbuild.OciFile{
						File: getString(sha256OciFileRaw, "file"),
						Repo: getString(sha256OciFileRaw, "repo"),
						Tag:  getString(sha256OciFileRaw, "tag"),
					}
				}

				binaries = append(binaries, artifact)
			}
		}
		buildReport.Binaries = binaries
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

// transformTektonStatus converts a map[string]any to a devbuild.TektonStatus
func transformTektonStatus(status map[string]any) *devbuild.TektonStatus {
	if status == nil {
		return nil
	}

	tektonStatus := &devbuild.TektonStatus{}

	// Transform pipelines
	if pipelinesRaw, ok := status["pipelines"].([]any); ok {
		pipelines := make([]*devbuild.TektonPipeline, 0, len(pipelinesRaw))
		for _, pipeRaw := range pipelinesRaw {
			if pipe, ok := pipeRaw.(map[string]any); ok {
				pipeline := &devbuild.TektonPipeline{
					EndAt:    getString(pipe, "endAt"),
					GitHash:  getString(pipe, "gitHash"),
					Name:     getString(pipe, "name"),
					Platform: getString(pipe, "platform"),
					StartAt:  getString(pipe, "startAt"),
					Status:   devbuild.BuildStatus(getString(pipe, "status")),
					URL:      getString(pipe, "url"),
				}

				// Transform images
				if imagesRaw, ok := pipe["images"].([]any); ok {
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
					pipeline.Images = images
				}

				// Transform OciArtifacts
				if ociArtifactsRaw, ok := pipe["ociArtifacts"].([]any); ok {
					ociArtifacts := make([]*devbuild.OciArtifact, 0, len(ociArtifactsRaw))
					for _, artifactRaw := range ociArtifactsRaw {
						if artifact, ok := artifactRaw.(map[string]any); ok {
							ociArtifact := &devbuild.OciArtifact{
								Repo: getString(artifact, "repo"),
								Tag:  getString(artifact, "tag"),
							}

							// Handle files array
							if filesRaw, ok := artifact["files"].([]any); ok {
								files := make([]string, 0, len(filesRaw))
								for _, fileRaw := range filesRaw {
									if file, ok := fileRaw.(string); ok {
										files = append(files, file)
									}
								}
								ociArtifact.Files = files
							}

							ociArtifacts = append(ociArtifacts, ociArtifact)
						}
					}
					pipeline.OciArtifacts = ociArtifacts
				}

				pipelines = append(pipelines, pipeline)
			}
		}
		tektonStatus.Pipelines = pipelines
	}

	return tektonStatus
}

// Helper function to safely get string value from a map
func getString(m map[string]any, key string) string {
	if val, ok := m[key].(string); ok {
		return val
	}
	return ""
}
