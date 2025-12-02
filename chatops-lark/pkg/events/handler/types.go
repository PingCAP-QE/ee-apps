package handler

import (
	"context"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

const (
	ctxKeyGithubToken     = "github_token"
	ctxKeyLarkSenderEmail = "lark.sender.email"

	// Message types
	msgTypePrivate = "private"
	msgTypeGroup   = "group"

	// Status enums
	StatusSuccess = "success"
	StatusFailure = "failure"
	StatusSkip    = "skip"
	StatusInfo    = "info"
)

type CommandHandler func(context.Context, []string) (string, error)

type CommandConfig struct {
	Description  string
	Handler      CommandHandler
	AuditWebhook string
	SetupContext func(ctx context.Context, cfg config.Config, sender *CommandActor) context.Context
}

type Command struct {
	Name   string
	Args   []string
	Sender *CommandActor
}

type CommandActor struct {
	OpenID   string
	Email    string
	GitHubID *string
}

type CommandResponse struct {
	Status  string
	Message string
	Error   string
}

type commandLarkMsgContent struct {
	Text string `json:"text"`
}

// Rich text (post) message structures
type postContent struct {
	ZhCn *postLanguage `json:"zh_cn,omitempty"`
	EnUs *postLanguage `json:"en_us,omitempty"`
	JaJp *postLanguage `json:"ja_jp,omitempty"`
}

type postLanguage struct {
	Title   string          `json:"title,omitempty"`
	Content [][]postElement `json:"content"`
}

type postElement struct {
	Tag      string `json:"tag"`
	Text     string `json:"text,omitempty"`
	UnEscape bool   `json:"un_escape,omitempty"`
	UserID   string `json:"user_id,omitempty"`
	UserName string `json:"user_name,omitempty"`
}
