package impl

import (
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// ImageInfo contains information about a Docker image artifact for notification.
type ImageInfo struct {
	Platform string
	URL      string
}

// BinaryInfo contains information about a binary artifact for notification.
type BinaryInfo struct {
	OciReference string
}

// NotificationInfo contains information for building a notification card.
type NotificationInfo struct {
	BuildID      int
	Product      string
	Version      string
	Status       string
	CreatedBy    string
	GitRef       string
	GithubRepo   string
	Platform     string
	ErrMsg       string
	PipelineRuns []PipelineRunInfo
	// Build report fields (populated on success)
	GitSha   string
	Images   []ImageInfo
	Binaries []BinaryInfo
}

// PipelineRunInfo contains information about a single pipeline run.
type PipelineRunInfo struct {
	Name   string
	Status string
	URL    string
}

// buildNotificationInfo builds a NotificationInfo from a build record.
func buildNotificationInfo(build *ent.DevBuild) *NotificationInfo {
	info := &NotificationInfo{
		BuildID:    build.ID,
		Product:    build.Product,
		Version:    build.Version,
		Status:     build.Status,
		CreatedBy:  build.CreatedBy,
		GitRef:     build.GitRef,
		GithubRepo: build.GithubRepo,
		Platform:   build.Platform,
		ErrMsg:     build.ErrMsg,
	}

	// Extract pipeline run info from TektonStatus
	if len(build.TektonStatus.Pipelines) > 0 {
		info.PipelineRuns = make([]PipelineRunInfo, len(build.TektonStatus.Pipelines))
		for i, p := range build.TektonStatus.Pipelines {
			info.PipelineRuns[i] = PipelineRunInfo{
				Name:   p.Name,
				Status: p.Status,
				URL:    p.URL,
			}
		}
	}

	// Extract build report data on success
	if build.Status == "SUCCESS" {
		if build.BuildReport.GitHash != "" {
			info.GitSha = build.BuildReport.GitHash
		}

		if len(build.BuildReport.Images) > 0 {
			images := make([]ImageInfo, 0, len(build.BuildReport.Images))
			for _, img := range build.BuildReport.Images {
				images = append(images, ImageInfo{
					Platform: img.Platform,
					URL:      img.URL,
				})
			}
			info.Images = images
		}

		for _, oci := range build.BuildReport.Binaries {
			for _, f := range oci.Files {
				info.Binaries = append(info.Binaries, BinaryInfo{
					OciReference: oci.Repo + ":" + oci.Tag + "/" + f,
				})
			}
		}
	}

	return info
}

// isTerminalStatus checks if the build status is terminal.
func isTerminalStatus(status string) bool {
	switch status {
	case "SUCCESS", "FAILURE", "ERROR", "ABORTED":
		return true
	default:
		return false
	}
}

// StatusColor returns the Lark card header template color for a status.
func StatusColor(status string) string {
	switch status {
	case "SUCCESS":
		return "green"
	case "FAILURE", "ERROR":
		return "red"
	case "ABORTED":
		return "orange"
	case "PROCESSING", "RUNNING":
		return "blue"
	default:
		return "grey"
	}
}

// PipelineStatusEmoji returns an emoji for a PipelineRun status.
// Uses Tekton's own condition Reason/Status values so users can distinguish
// between Failed, Cancelled, TimedOut, Skipped, etc. at a glance.
func PipelineStatusEmoji(status string) string {
	switch status {
	case "Succeeded", "Completed", "True":
		return "✅"
	case "Failed", "False":
		return "❌"
	case "Cancelled":
		return "🚫"
	case "Skipped":
		return "⏭️"
	case "TimedOut":
		return "⏰"
	case "ExceededNodeResources":
		return "💥"
	case "Started":
		return "🚀"
	case "Running", "Unknown":
		return "🔄"
	default:
		return "⏳"
	}
}

// StatusEmoji returns an emoji for the build status.
func StatusEmoji(status string) string {
	switch status {
	case "SUCCESS":
		return "✅"
	case "FAILURE", "ERROR":
		return "❌"
	case "ABORTED":
		return "🚫"
	case "PROCESSING", "RUNNING":
		return "🔄"
	default:
		return "⏳"
	}
}

// Ensure type implements Notifier at compile time.
var _ Notifier = (*LarkNotifier)(nil)
