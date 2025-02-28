package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/audit"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/response"
	"github.com/allegro/bigcache/v3"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcontact "github.com/larksuite/oapi-sdk-go/v3/service/contact/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
)

const (
	ctxKeyGithubToken     = "github_token"
	ctxKeyLarkSenderEmail = "lark.sender.email"

	// Message types
	msgTypePrivate = "private"
	msgTypeGroup   = "group"
)

var (
	commandList = []string{
		"/approve_pr",                   // rarely used
		"/build_hotfix",                 // rarely used
		"/check_docker_image_identical", // rarely used
		"/check_pr_label",               // rarely used
		"/cherry-pick-invite",
		"/create_branch_from_tag",
		"/create_hotfix_branch",
		"/create_milestone",              // rarely used
		"/create_new_branch_from_branch", // rarely used
		"/create_product_release",        // rarely used
		"/create_tag_from_branch",
		"/devbuild",
		"/label_pr",
		"/merge_pr", // rarely used
		"/sync_docker_image",
		"/trigger_job", // rarely used
	}
	privilegedCommandList = []string{
		// "/approve_pr",
		// "/label_pr",
		// "/check_pr_label",
		// "/create_milestone",
		// "/merge_pr",
		// "/create_tag_from_branch",
		// "/trigger_job",
		// "/create_branch_from_tag",
		// "/create_new_branch_from_branch",
		// "/build_hotfix",
		// "/sync_docker_image",
		// "/create_product_release",
		// "/create_hotfix_branch",
	}
)

type Command struct {
	Name   string
	Args   []string
	Sender *CommandSender
}

type CommandSender struct {
	OpenID string
	Email  string
}

type CommandResponse struct {
	Status  string
	Message string
	Error   string
}

type commandLarkMsgContent struct {
	Text string `json:"text"`
}

type rootHandler struct {
	*lark.Client
	eventCache *bigcache.BigCache
	Config     map[string]any
}

// InformationError represents an information level error that occurred during command execution.
// it will give some information but not error, such as help and skip reasons.
type InformationError error

// SkipError represents a skip level error that occurred during command execution.
type SkipError error

// Status enums
const (
	StatusSuccess = "success"
	StatusFailure = "failure"
	StatusSkip    = "skip"
	StatusInfo    = "info"
)

func NewRootForMessage(respondCli *lark.Client, cfg map[string]any) func(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	cacheCfg := bigcache.DefaultConfig(10 * time.Minute)
	cacheCfg.Logger = &log.Logger
	cache, _ := bigcache.New(context.Background(), cacheCfg)
	h := &rootHandler{Client: respondCli, Config: cfg, eventCache: cache}
	return h.Handle
}

func (r *rootHandler) Handle(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	eventID := event.EventV2Base.Header.EventID

	// check if the event has been handled.
	_, err := r.eventCache.Get(eventID)
	if err == nil {
		log.Debug().Str("eventId", eventID).Msg("event already handled")
		return nil
	} else if err == bigcache.ErrEntryNotFound {
		r.eventCache.Set(eventID, []byte(eventID))
	} else {
		log.Err(err).Msg("unexpected error")
		return err
	}

	hl := log.With().Str("eventId", eventID).Logger()

	// Get bot name from config with type assertion
	botName := ""
	if name, ok := r.Config["bot_name"].(string); ok {
		botName = name
	}

	command := shouldHandle(event, botName)
	if command == nil {
		hl.Debug().Msg("none command received")
		return nil
	}

	hl.Debug().Str("command", command.Name).Any("args", command.Args).Msg("received command")
	refactionID, err := r.addReaction(*event.Event.Message.MessageId)
	if err != nil {
		hl.Err(err).Msg("send heartbeat failed")
		return err
	}

	senderOpenID := *event.Event.Sender.SenderId.OpenId
	res, err := r.Contact.User.Get(ctx, larkcontact.NewGetUserReqBuilder().
		UserIdType(larkcontact.UserIdTypeOpenId).
		UserId(senderOpenID).
		Build())
	if err != nil {
		hl.Err(err).Msg("get user info failed.")
		return err
	}

	if !res.Success() {
		hl.Error().Bytes("body", res.RawBody).Msg("get user info failed")
		return nil
	}
	hl.Debug().Bytes("body", res.RawBody).Msg("get user info success")

	command.Sender = &CommandSender{OpenID: senderOpenID, Email: *res.Data.User.Email}

	go func() {
		defer r.deleteReaction(*event.Event.Message.MessageId, refactionID)
		message, err := r.handleCommand(ctx, command)
		if err == nil {
			r.sendResponse(*event.Event.Message.MessageId, StatusSuccess, message)
			return
		}

		// send different level response for the error types.
		switch e := err.(type) {
		case SkipError:
			message = fmt.Sprintf("%s\n---\n**skip:**\n%v", message, e)
			r.sendResponse(*event.Event.Message.MessageId, StatusSkip, message)
		case InformationError:
			message = fmt.Sprintf("%s\n---\n**information:**\n%v", message, e)
			r.sendResponse(*event.Event.Message.MessageId, StatusInfo, message)
		default:
			message = fmt.Sprintf("%s\n---\n**error:**\n%v", message, err)
			r.sendResponse(*event.Event.Message.MessageId, StatusFailure, message)
		}
	}()

	return nil
}

func (r *rootHandler) handleCommand(ctx context.Context, command *Command) (string, error) {
	switch command.Name {
	case "/devbuild":
		runCtx := context.WithValue(ctx, ctxKeyLarkSenderEmail, command.Sender.Email)
		return runCommandDevbuild(runCtx, command.Args)
	case "/cherry-pick-invite":
		runCtx := context.WithValue(ctx, ctxKeyGithubToken, r.Config["cherry-pick-invite.github_token"])

		ret, err := runCommandCherryPickInvite(runCtx, command.Args)
		if err == nil {
			auditCtx := context.WithValue(ctx, "audit_webhook", r.Config["cherry-pick-invite.audit_webhook"])
			_ = r.audit(auditCtx, command)
		}
		return ret, err
	case "/create_hotfix_branch":
		return runCommandHotfixCreateBranch(ctx, command.Args)
	default:
		return "", fmt.Errorf("not support command: %s", command.Name)
	}
}

func (r *rootHandler) audit(ctx context.Context, command *Command) error {
	auditWebhook := ctx.Value("audit_webhook")
	if auditWebhook == nil {
		return nil
	}

	auditInfo := &audit.AuditInfo{
		UserEmail: command.Sender.Email,
		Command:   command.Name,
		Args:      command.Args,
	}

	return audit.RecordAuditMessage(auditInfo, auditWebhook.(string))
}

func (r *rootHandler) sendResponse(reqMsgID string, status, message string) error {
	replyMsg, err := response.NewReplyMessageReq(reqMsgID, status, message)
	if err != nil {
		log.Err(err).Msg("render msg faled.")
		return err
	}

	res, err := r.Im.Message.Reply(context.Background(), replyMsg)
	if err != nil {
		log.Err(err).Msg("send msg error.")
		return err
	}

	if !res.Success() {
		log.Error().Msg("response message sent failed")
	}

	return nil
}

func (r *rootHandler) addReaction(reqMsgID string) (string, error) {
	req := larkim.NewCreateMessageReactionReqBuilder().
		MessageId(reqMsgID).
		Body(larkim.NewCreateMessageReactionReqBodyBuilder().
			ReactionType(larkim.NewEmojiBuilder().EmojiType("OnIt").Build()).
			Build()).
		Build()

	res, err := r.Im.MessageReaction.Create(context.Background(), req)
	if err != nil {
		log.Err(err).Msg("send reaction failed.")
		return "", err
	}
	if !res.Success() {
		log.Error().Msg("send reaction failed")
		return "", nil
	}

	return *res.Data.ReactionId, nil
}

func (r *rootHandler) deleteReaction(reqMsgID, reactionID string) error {
	req := larkim.NewDeleteMessageReactionReqBuilder().
		MessageId(reqMsgID).
		ReactionId(reactionID).
		Build()

	if _, err := r.Im.MessageReaction.Delete(context.Background(), req); err != nil {
		log.Err(err).Msg("delete reaction failed.")
		return err
	}

	return nil
}

// determineMessageType determines the message type and checks if the bot was mentioned
func determineMessageType(event *larkim.P2MessageReceiveV1, botName string) (msgType string, chatID string, isMentionBot bool) {
	// Check if the message is from a group chat
	if event.Event.Message.ChatType != nil && *event.Event.Message.ChatType == "group" {
		msgType = msgTypeGroup
		if event.Event.Message.ChatId != nil {
			chatID = *event.Event.Message.ChatId
		}
		isMentionBot = checkIfBotMentioned(event, botName)
	} else {
		msgType = msgTypePrivate
	}

	return msgType, chatID, isMentionBot
}

// checkIfBotMentioned checks if the bot was mentioned in the message
func checkIfBotMentioned(event *larkim.P2MessageReceiveV1, botName string) bool {
	if event.Event.Message.Mentions == nil || len(event.Event.Message.Mentions) == 0 {
		return false
	}

	for _, mention := range event.Event.Message.Mentions {
		if mention == nil {
			continue
		}
		if mention.Name != nil {
			// check if the bot was mentioned
			if *mention.Name == botName {
				return true
			}
		}
	}
	return false
}

// parseGroupCommand parses commands from group messages
// Supported format: @bot /command arg1 arg2...
func parseGroupCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		return nil
	}

	// In Lark messages, mentions are converted to @_user_X format
	// Use regex to match commands after @_user_X: /command arg1 arg2...
	re := regexp.MustCompile(`@_user_\d+\s+(/\S+)(.*)`)
	matches := re.FindStringSubmatch(content.Text)

	if len(matches) < 3 {
		return nil
	}

	commandName := matches[1]
	argsStr := strings.TrimSpace(matches[2])
	var args []string
	if argsStr != "" {
		args = strings.Fields(argsStr)
	}

	// Check if it's a privileged command
	if slices.Contains(privilegedCommandList, commandName) {
		return nil
	}

	return &Command{Name: commandName, Args: args}
}

// parsePrivateCommand parses commands from private messages
func parsePrivateCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		return nil
	}

	messageParts := strings.Fields(content.Text)
	if len(messageParts) < 1 || slices.Contains(privilegedCommandList, messageParts[0]) {
		return nil
	}

	return &Command{Name: messageParts[0], Args: messageParts[1:]}
}

func shouldHandle(event *larkim.P2MessageReceiveV1, botName string) *Command {
	// Determine message type and whether the bot was mentioned
	msgType, _, isMentionBot := determineMessageType(event, botName)

	if msgType == msgTypeGroup && isMentionBot {
		return parseGroupCommand(event)
	} else if msgType == msgTypePrivate {
		return parsePrivateCommand(event)
	}
	return nil
}
