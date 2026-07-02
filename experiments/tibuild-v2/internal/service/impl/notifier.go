package impl

import (
	"context"
	"crypto/sha1"
	"fmt"
	"sync/atomic"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// Notifier defines the interface for sending build notifications.
type Notifier interface {
	Notify(ctx context.Context, build *ent.DevBuild) error
}

// LarkNotifier sends Lark IM messages when builds reach terminal state.
// The client is stored atomically to support safe runtime updates via config reload.
type LarkNotifier struct {
	client atomic.Value // stores *larksdk.Client
	logger *zerolog.Logger
}

// NewLarkNotifier creates a new LarkNotifier with the given Lark app credentials.
func NewLarkNotifier(appID, appSecret string, logger *zerolog.Logger) *LarkNotifier {
	n := &LarkNotifier{logger: logger}
	n.client.Store(newLarkClient(appID, appSecret))
	return n
}

// Reload updates the Lark client with new credentials. Safe to call concurrently with Notify.
func (n *LarkNotifier) Reload(appID, appSecret string) {
	n.client.Store(newLarkClient(appID, appSecret))
}

// Disable clears the Lark client, effectively disabling notifications.
func (n *LarkNotifier) Disable() {
	n.client.Store((*larksdk.Client)(nil))
}

// Notify sends a Lark notification for the given build to the createdBy user.
func (n *LarkNotifier) Notify(ctx context.Context, build *ent.DevBuild) error {
	client := n.client.Load().(*larksdk.Client)
	if client == nil {
		n.logger.Debug().Msg("lark client not configured, skipping notification")
		return nil
	}

	receiver := build.CreatedBy
	if receiver == "" {
		n.logger.Debug().Int("build_id", build.ID).Msg("createdBy is empty, skipping notification")
		return nil
	}

	cardStr, err := NewLarkCardJSON(buildNotificationInfo(build))
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to build notification card")
		return err
	}

	req := larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(getLarkReceiverIDType(receiver)).
		Body(larkim.NewCreateMessageReqBodyBuilder().
			MsgType(larkim.MsgTypeInteractive).
			ReceiveId(receiver).
			Content(cardStr).
			Uuid(newLarkMsgUUID(build.ID, receiver)).
			Build()).
		Build()

	resp, err := client.Im.Message.Create(ctx, req)
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to send lark message")
		return err
	}
	if !resp.Success() {
		n.logger.Error().
			Int("build_id", build.ID).
			Str("request_id", resp.RequestId()).
			Int("code", resp.Code).
			Str("msg", resp.Msg).
			Msg("lark message API returned error")
		return fmt.Errorf("lark message API error: code=%d msg=%s", resp.Code, resp.Msg)
	}

	n.logger.Info().
		Int("build_id", build.ID).
		Str("receiver", receiver).
		Str("message_id", *resp.Data.MessageId).
		Msg("lark notification sent successfully")
	return nil
}

// newLarkMsgUUID generates a deterministic UUID for idempotency.
func newLarkMsgUUID(buildID int, receiver string) string {
	h := sha1.New()
	fmt.Fprintf(h, "devbuild-%d-%s", buildID, receiver)
	return fmt.Sprintf("%x", h.Sum(nil))
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
