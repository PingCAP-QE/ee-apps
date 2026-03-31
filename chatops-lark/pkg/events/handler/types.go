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
	Audit        *config.AuditConfig
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
