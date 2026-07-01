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

	return NewLarkCardWithGoTemplate(info)
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
	case "success", "failure", "error", "aborted":
		return true
	default:
		return false
	}
}

// StatusColor returns the Lark card header template color for a status.
func StatusColor(status string) string {
	switch status {
	case "success":
		return "green"
	case "failure", "error":
		return "red"
	case "aborted":
		return "orange"
	case "running", "processing":
		return "blue"
	default:
		return "grey"
	}
}

// StatusEmoji returns an emoji for the build status.
func StatusEmoji(status string) string {
	switch status {
	case "success":
		return "✅"
	case "failure", "error":
		return "❌"
	case "aborted":
		return "🚫"
	case "running", "processing":
		return "🔄"
	default:
		return "⏳"
	}
}
