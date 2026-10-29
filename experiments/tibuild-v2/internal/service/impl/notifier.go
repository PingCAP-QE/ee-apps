package impl

import (
	"context"
	"crypto/sha1"
	"encoding/json"
	"errors"
	"fmt"
	"sync/atomic"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// Notifier defines the interface for sending build notifications.
// Returns the updated notification state for persistence.
type Notifier interface {
	Notify(ctx context.Context, build *ent.DevBuild) (*schema.NotificationState, error)
}

// LarkNotifier sends Lark IM messages for each configured channel.
// Each channel's first send creates a card; subsequent updates refresh it in place.
type LarkNotifier struct {
	client   atomic.Value // stores *larksdk.Client
	channels []config.LarkChannel
	logger   *zerolog.Logger
	options  atomic.Value // notificationOptions
}

type notificationOptions struct {
	DownloadURL  string
	ImageSyncURL string
}

func (n *LarkNotifier) configureArtifacts(cfg *config.Service) {
	n.options.Store(notificationOptions{DownloadURL: cfg.Tekton.OciFileDownloadURL, ImageSyncURL: cfg.Lark.ImageSyncURL})
}

// NewLarkNotifier creates a new LarkNotifier with Lark app credentials and optional channels.
func NewLarkNotifier(appID, appSecret string, channels []config.LarkChannel, logger *zerolog.Logger) *LarkNotifier {
	n := &LarkNotifier{
		channels: channels,
		logger:   logger,
	}
	n.client.Store(newLarkClient(appID, appSecret))
	n.options.Store(notificationOptions{})
	return n
}

// Reload updates credentials and channels at runtime.
func (n *LarkNotifier) Reload(appID, appSecret string, channels []config.LarkChannel) {
	n.client.Store(newLarkClient(appID, appSecret))
	n.channels = channels
}

// Disable clears the Lark client, effectively disabling notifications.
func (n *LarkNotifier) Disable() {
	n.client.Store((*larksdk.Client)(nil))
}

// Notify delivers or updates cards for all channels and returns the updated state.
func (n *LarkNotifier) Notify(ctx context.Context, build *ent.DevBuild) (*schema.NotificationState, error) {
	client := n.client.Load().(*larksdk.Client)
	if client == nil {
		n.logger.Debug().Msg("lark client not configured, skipping notification")
		return nil, nil
	}

	info := buildNotificationInfo(build)
	options := n.options.Load().(notificationOptions)
	info.ImageSyncURL = options.ImageSyncURL
	for i := range info.Binaries {
		bin := &info.Binaries[i]
		bin.URL = notificationDownloadURL(options.DownloadURL, bin.OciReference)
		bin.Sha256URL = notificationDownloadURL(options.DownloadURL, bin.Sha256Reference)
	}
	cardStr, err := NewLarkCardJSON(info)
	if err != nil {
		n.logger.Err(err).Int("build_id", build.ID).Msg("failed to build notification card")
		return nil, err
	}

	state := build.NotificationState
	changed := false

	// --- DM to build creator (identified by empty ChatID) ---
	if build.CreatedBy != "" {
		dm := findLarkState(&state, "")
		newID, err := n.sendOrUpdate(ctx, client, build.ID, build.CreatedBy, dm.MessageID, cardStr)
		if err != nil {
			n.logger.Err(err).Int("build_id", build.ID).Msg("lark DM notification failed")
		} else if newID != dm.MessageID {
			dm.MessageID = newID
			changed = true
		}
	}

	// --- Group chat channels ---
	for _, ch := range n.channels {
		cur := findLarkState(&state, ch.ChatID)
		newID, err := n.sendOrUpdate(ctx, client, build.ID, ch.ChatID, cur.MessageID, cardStr)
		if err != nil {
			n.logger.Err(err).Int("build_id", build.ID).Str("channel", ch.Name).Msg("lark channel notification failed")
		} else if newID != cur.MessageID {
			cur.MessageID = newID
			cur.ChatID = ch.ChatID
			changed = true
		}
	}

	if !changed {
		return nil, nil
	}
	return &state, nil
}

// sendOrUpdate creates a new card or updates an existing one.
// Returns the current (or new) message ID.
func (n *LarkNotifier) sendOrUpdate(ctx context.Context, client *larksdk.Client, buildID int, receiver, existingMsgID, cardStr string) (string, error) {
	if existingMsgID != "" {
		// Update interactive card via PATCH (PUT does not support interactive cards)
		req := larkim.NewPatchMessageReqBuilder().
			MessageId(existingMsgID).
			Body(&larkim.PatchMessageReqBody{Content: &cardStr}).
			Build()

		resp, err := client.Im.Message.Patch(ctx, req)
		if err == nil && resp.Success() {
			n.logger.Debug().Int("build_id", buildID).Str("message_id", existingMsgID).Msg("card updated")
			return existingMsgID, nil
		}
		if err == nil {
			n.logger.Warn().Int("build_id", buildID).Int("code", resp.Code).Str("msg", resp.Msg).Msg("update failed, will try create")
		} else {
			n.logger.Err(err).Int("build_id", buildID).Msg("update request failed, will try create")
		}
	}

	// Create new card
	req := larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(getLarkReceiverIDType(receiver)).
		Body(larkim.NewCreateMessageReqBodyBuilder().
			MsgType(larkim.MsgTypeInteractive).
			ReceiveId(receiver).
			Content(cardStr).
			Uuid(newLarkMsgUUID(buildID, receiver)).
			Build()).
		Build()

	resp, err := client.Im.Message.Create(ctx, req)
	if err != nil {
		return "", err
	}
	if !resp.Success() {
		return "", fmt.Errorf("lark create message error: code=%d msg=%s", resp.Code, resp.Msg)
	}

	n.logger.Info().Int("build_id", buildID).Str("receiver", receiver).Str("msg_id", *resp.Data.MessageId).Msg("card created")
	return *resp.Data.MessageId, nil
}

// findLarkState returns the LarkMessageState for a given ChatID, creating one if missing.
// Use ChatID="" to get/create the DM state (receiver = build creator).
func findLarkState(state *schema.NotificationState, chatID string) *schema.LarkMessageState {
	for i := range state.Lark {
		if state.Lark[i].ChatID == chatID {
			return &state.Lark[i]
		}
	}
	state.Lark = append(state.Lark, schema.LarkMessageState{ChatID: chatID})
	return &state.Lark[len(state.Lark)-1]
}

// newLarkMsgUUID generates a deterministic idempotency key.
func newLarkMsgUUID(buildID int, receiver string) string {
	h := sha1.New()
	fmt.Fprintf(h, "devbuild-%d-%s", buildID, receiver)
	return fmt.Sprintf("%x", h.Sum(nil))
}

// SendAlert sends a plain-text alert to every configured Lark channel.
func (n *LarkNotifier) SendAlert(ctx context.Context, title, body string) error {
	client := n.client.Load().(*larksdk.Client)
	if client == nil {
		n.logger.Debug().Msg("lark client not configured, skipping alert")
		return nil
	}
	content, err := json.Marshal(map[string]string{"text": title + "\n" + body})
	if err != nil {
		return err
	}

	var errs []error
	for _, channel := range n.channels {
		req := larkim.NewCreateMessageReqBuilder().
			ReceiveIdType(getLarkReceiverIDType(channel.ChatID)).
			Body(larkim.NewCreateMessageReqBodyBuilder().
				MsgType(larkim.MsgTypeText).
				ReceiveId(channel.ChatID).
				Content(string(content)).
				Build()).
			Build()

		resp, err := client.Im.Message.Create(ctx, req)
		if err != nil {
			errs = append(errs, fmt.Errorf("channel %s: %w", channel.Name, err))
			continue
		}
		if !resp.Success() {
			errs = append(errs, fmt.Errorf("channel %s: lark code=%d msg=%s", channel.Name, resp.Code, resp.Msg))
		}
	}
	return errors.Join(errs...)
}
