package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"text/template"
	"time"
)

// Notifier defines the interface for sending build completion notifications.
type Notifier interface {
	// Notify sends a notification for a completed build.
	// It should log errors and not block the caller on failure.
	Notify(ctx context.Context, build *DevBuild) error
}

// NotificationInfo contains the information needed for a build notification.
type NotificationInfo struct {
	Product    string
	Version    string
	Status     string
	Platform   string
	ViewURLs   []string
	CreatedBy  string
	BuildID    int
	IsHotfix   bool
}

// LarkNotifier sends notifications to a Lark webhook.
type LarkNotifier struct {
	webhookURL string
	httpClient *http.Client
	enabled    bool
}

// LarkCardMessage represents a Lark interactive card message.
type LarkCardMessage struct {
	MsgType string      `json:"msg_type"`
	Card    LarkCard    `json:"card"`
}

// LarkCard represents the card structure.
type LarkCard struct {
	Header   LarkCardHeader   `json:"header"`
	Elements []LarkCardElement `json:"elements"`
}

// LarkCardHeader represents the card header.
type LarkCardHeader struct {
	Title    LarkCardTitle `json:"title"`
	Template string        `json:"template,omitempty"`
}

// LarkCardTitle represents the card title.
type LarkCardTitle struct {
	Content string `json:"content"`
	Tag     string `json:"tag"`
}

// LarkCardElement represents a card element.
type LarkCardElement struct {
	Tag     string        `json:"tag"`
	Content string        `json:"content,omitempty"`
	Text    *LarkCardText `json:"text,omitempty"`
	Fields  []LarkField   `json:"fields,omitempty"`
}

// LarkCardText represents text in a card element.
type LarkCardText struct {
	Content string `json:"content"`
	Tag     string `json:"tag"`
}

// LarkField represents a field in a card element.
type LarkField struct {
	IsShort bool          `json:"is_short"`
	Text    LarkCardText  `json:"text"`
}

// NewLarkNotifier creates a new LarkNotifier.
func NewLarkNotifier(webhookURL string, enabled bool) *LarkNotifier {
	return &LarkNotifier{
		webhookURL: webhookURL,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		enabled: enabled,
	}
}

// Notify sends a notification for a completed build.
func (n *LarkNotifier) Notify(ctx context.Context, build *DevBuild) error {
	if !n.enabled {
		return nil
	}

	info := &NotificationInfo{
		Product:   string(build.Spec.Product),
		Version:   build.Spec.Version,
		Status:    build.Status.Status,
		Platform:  build.Spec.Platform,
		ViewURLs:  build.Status.PipelineViewURLs,
		CreatedBy: build.Meta.CreatedBy,
		BuildID:   build.ID,
		IsHotfix:  build.Spec.IsHotfix,
	}

	card, err := buildLarkCard(info)
	if err != nil {
		return fmt.Errorf("build lark card: %w", err)
	}

	msg := LarkCardMessage{
		MsgType: "interactive",
		Card:    card,
	}

	body, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal message: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, n.webhookURL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := n.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	return nil
}

// StatusToTemplate returns the Lark card template color based on build status.
func StatusToTemplate(status string) string {
	switch status {
	case BuildStatusSuccess:
		return "green"
	case BuildStatusFailure, BuildStatusError:
		return "red"
	case BuildStatusAborted:
		return "orange"
	default:
		return "blue"
	}
}

// StatusToEmoji returns an emoji for the build status.
func StatusToEmoji(status string) string {
	switch status {
	case BuildStatusSuccess:
		return "✅"
	case BuildStatusFailure:
		return "❌"
	case BuildStatusError:
		return "⚠️"
	case BuildStatusAborted:
		return "🚫"
	default:
		return "⏳"
	}
}

const larkCardTemplate = `{
  "header": {
    "title": {
      "content": "{{.StatusEmoji}} DevBuild {{.Status}} - {{.Product}} {{.Version}}",
      "tag": "plain_text"
    },
    "template": "{{.Template}}"
  },
  "elements": [
    {
      "tag": "div",
      "fields": [
        {
          "is_short": true,
          "text": {
            "content": "**Product:** {{.Product}}",
            "tag": "lark_md"
          }
        },
        {
          "is_short": true,
          "text": {
            "content": "**Version:** {{.Version}}",
            "tag": "lark_md"
          }
        },
        {
          "is_short": true,
          "text": {
            "content": "**Status:** {{.Status}}",
            "tag": "lark_md"
          }
        },
        {
          "is_short": true,
          "text": {
            "content": "**Platform:** {{.Platform}}",
            "tag": "lark_md"
          }
        },
        {
          "is_short": true,
          "text": {
            "content": "**Created By:** {{.CreatedBy}}",
            "tag": "lark_md"
          }
        },
        {
          "is_short": true,
          "text": {
            "content": "**Hotfix:** {{.IsHotfix}}",
            "tag": "lark_md"
          }
        }
      ]
    },
    {{- if .ViewURLs}}
    {
      "tag": "div",
      "text": {
        "content": "**Pipeline URLs:**\n{{range .ViewURLs}}• [View Pipeline]({{.}})\n{{end}}",
        "tag": "lark_md"
      }
    },
    {{- end}}
    {
      "tag": "note",
      "elements": [
        {
          "content": "Build ID: {{.BuildID}} | {{.StatusEmoji}} {{.Status}}",
          "tag": "lark_md"
        }
      ]
    }
  ]
}`

func buildLarkCard(info *NotificationInfo) (LarkCard, error) {
	tmpl, err := template.New("lark").Parse(larkCardTemplate)
	if err != nil {
		return LarkCard{}, fmt.Errorf("parse template: %w", err)
	}

	data := struct {
		*NotificationInfo
		StatusEmoji string
		Template    string
	}{
		NotificationInfo: info,
		StatusEmoji:      StatusToEmoji(info.Status),
		Template:         StatusToTemplate(info.Status),
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return LarkCard{}, fmt.Errorf("execute template: %w", err)
	}

	var card LarkCard
	if err := json.Unmarshal(buf.Bytes(), &card); err != nil {
		return LarkCard{}, fmt.Errorf("unmarshal card: %w", err)
	}

	return card, nil
}
