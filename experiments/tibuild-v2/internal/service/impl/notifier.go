package impl

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// Notifier defines the interface for sending build notifications.
type Notifier interface {
	Notify(ctx context.Context, build *ent.DevBuild) error
}

// LarkNotifier sends Lark webhook notifications when builds reach terminal state.
// The webhookURL is stored atomically to support safe runtime updates via config reload.
type LarkNotifier struct {
	webhookURL atomic.Value // stores string
	httpClient *http.Client
	logger     *zerolog.Logger
}

// NewLarkNotifier creates a new LarkNotifier.
func NewLarkNotifier(webhookURL string, logger *zerolog.Logger) *LarkNotifier {
	n := &LarkNotifier{
		httpClient: &http.Client{Timeout: 10 * time.Second},
		logger:     logger,
	}
	n.webhookURL.Store(webhookURL)
	return n
}

// SetWebhookURL updates the webhook URL atomically. Safe to call concurrently with Notify.
func (n *LarkNotifier) SetWebhookURL(url string) {
	n.webhookURL.Store(url)
}

// Notify sends a Lark notification for the given build.
func (n *LarkNotifier) Notify(ctx context.Context, build *ent.DevBuild) error {
	webhookURL := n.webhookURL.Load().(string)
	if webhookURL == "" {
		n.logger.Debug().Msg("lark webhook URL not configured, skipping notification")
		return nil
	}

	payload, err := buildNotificationCard(build)
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to build notification card")
		return err
	}

	body, err := json.Marshal(payload)
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to marshal notification payload")
		return err
	}

	n.logger.Debug().Str("webhook", webhookURL).Str("body", string(body)).Msg("debug lark message")

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, webhookURL, bytes.NewReader(body))
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to create notification request")
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := n.httpClient.Do(req)
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to send notification")
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		n.logger.Error().Int("build_id", build.ID).Int("status_code", resp.StatusCode).Msg("notification request failed")
		return fmt.Errorf("notification request failed with status %d", resp.StatusCode)
	}

	n.logger.Info().Int("build_id", build.ID).Str("status", build.Status).Msg("notification sent successfully")
	return nil
}

// buildNotificationCard builds a Lark interactive card for the build notification.
func buildNotificationCard(build *ent.DevBuild) (map[string]any, error) {
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

		// Extract images (platform + URL)
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

		// Extract binaries (OCI references: repo:tag/file)
		for _, oci := range build.BuildReport.Binaries {
			for _, f := range oci.Files {
				info.Binaries = append(info.Binaries, BinaryInfo{
					OciReference: oci.Repo + ":" + oci.Tag + "/" + f,
				})
			}
		}
	}

	return NewLarkCardWithGoTemplate(info)
}

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
