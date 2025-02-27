package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
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
	"github.com/rs/zerolog"
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
	Name    string
	Args    []string
	Sender  *CommandSender
	MsgType string // Message type: private or group
	ChatID  string // Group ID, only valid when MsgType is group
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
	botName    string // Bot name, for @bot mention in group chat
	logger     zerolog.Logger
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

	h := &rootHandler{
		Client:     respondCli,
		Config:     cfg,
		eventCache: cache,
		logger:     log.Logger,
	}

	// Synchronously fetch bot information during initialization
	if err := h.fetchBotInfo(context.Background()); err != nil {
		h.logger.Fatal().Err(err).Msg("failed to fetch bot info during initialization, exiting")
		os.Exit(1)
	}

	return h.Handle
}

// fetchBotInfo retrieves the bot information and sets the botName
func (r *rootHandler) fetchBotInfo(ctx context.Context) error {
	maxRetries := 3
	retryInterval := 2 * time.Second

	for i := 0; i < maxRetries; i++ {
		if i > 0 {
			r.logger.Info().Int("retry", i).Msg("retrying to fetch bot info")
			time.Sleep(retryInterval)
		}
		// Get bot name for detect mention bot in group chat
		// Note: In Lark API, @bot will be automatically converted to @_user_X format in message content
		if botName, ok := r.Config["bot_name"].(string); ok && botName != "" {
			r.botName = botName
			r.logger.Info().Str("botName", r.botName).Msg("successfully set bot name which is from API")
			return nil
		}
	}

	return fmt.Errorf("failed to get bot name: bot_name not found in API")
}

func (r *rootHandler) Handle(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	eventID := event.EventV2Base.Header.EventID
	r.logger = log.With().Str("eventId", eventID).Logger()

	// Check if the event has already been processed
	if r.isEventAlreadyHandled(eventID) {
		return nil
	}

	// Determine message type and whether the bot was mentioned
	msgType, chatID, isMentionBot := r.determineMessageType(event)

	command := r.parseCommand(event, msgType, isMentionBot)
	if command == nil {
		r.logger.Debug().Msg("none command received")
		return nil
	}

	command.MsgType = msgType
	command.ChatID = chatID

	// Get sender information and process command
	return r.processCommand(ctx, event, command)
}

// isEventAlreadyHandled checks if the event has already been processed
func (r *rootHandler) isEventAlreadyHandled(eventID string) bool {
	_, err := r.eventCache.Get(eventID)
	if err == nil {
		r.logger.Debug().Str("eventId", eventID).Msg("event already handled")
		return true
	} else if err == bigcache.ErrEntryNotFound {
		r.eventCache.Set(eventID, []byte(eventID))
		return false
	} else {
		log.Err(err).Msg("unexpected error")
		return true
	}
}

// determineMessageType determines the message type and checks if the bot was mentioned
func (r *rootHandler) determineMessageType(event *larkim.P2MessageReceiveV1) (msgType string, chatID string, isMentionBot bool) {
	// Check if the message is from a group chat
	if event.Event.Message.ChatId != nil && event.Event.Message.ChatType != nil && *event.Event.Message.ChatType == "group" {
		msgType = msgTypeGroup
		chatID = *event.Event.Message.ChatId
		isMentionBot = r.checkIfBotMentioned(event)

		// Print mention bot information
		if isMentionBot {
			r.logger.Debug().Str("chatID", chatID).Msg("mention the bot from group chat")
		} else {
			r.logger.Debug().Str("chatID", chatID).Msg("not mention the bot from group chat, ignore...")
		}
	} else {
		msgType = msgTypePrivate
		r.logger.Debug().Msg("received message from private chat")
	}

	return msgType, chatID, isMentionBot
}

// checkIfBotMentioned checks if the bot was mentioned in the message
func (r *rootHandler) checkIfBotMentioned(event *larkim.P2MessageReceiveV1) bool {
	if event.Event.Message.Mentions == nil || len(event.Event.Message.Mentions) == 0 {
		r.logger.Debug().Msg("no mentions in the message")
		return false
	}

	for i, mention := range event.Event.Message.Mentions {
		if mention == nil {
			continue
		}

		logger := r.logger.Debug().Int("index", i)

		if mention.Name != nil {
			logger = logger.Str("name", *mention.Name)
			// check if the bot was mentioned
			if *mention.Name == r.botName {
				logger.Msg("bot mentioned")
				return true
			}
		}

		logger.Msg("mention info")
	}

	r.logger.Debug().Msg("bot not mentioned in the message")
	return false
}

// parseCommand parses the command based on message type
func (r *rootHandler) parseCommand(event *larkim.P2MessageReceiveV1, msgType string, isMentionBot bool) *Command {
	if msgType == msgTypeGroup && isMentionBot {
		return r.parseGroupCommand(event)
	} else if msgType == msgTypePrivate {
		return r.parsePrivateCommand(event)
	}
	return nil
}

// processCommand processes the command and sends a response
func (r *rootHandler) processCommand(ctx context.Context, event *larkim.P2MessageReceiveV1, command *Command) error {
	r.logger.Debug().Str("command", command.Name).Any("args", command.Args).Msg("received command")

	// Add reaction emoji
	refactionID, err := r.addReaction(*event.Event.Message.MessageId)
	if err != nil {
		r.logger.Err(err).Msg("send heartbeat reaction failed")
		return err
	}

	// Get sender information
	senderOpenID := *event.Event.Sender.SenderId.OpenId
	res, err := r.Contact.User.Get(ctx, larkcontact.NewGetUserReqBuilder().
		UserIdType(larkcontact.UserIdTypeOpenId).
		UserId(senderOpenID).
		Build())
	if err != nil {
		r.logger.Err(err).Msg("get user info failed.")
		return err
	}

	if !res.Success() {
		r.logger.Error().Bytes("body", res.RawBody).Msg("get user info failed")
		return nil
	}
	r.logger.Debug().Bytes("body", res.RawBody).Msg("get user info success")

	command.Sender = &CommandSender{OpenID: senderOpenID, Email: *res.Data.User.Email}

	// Asynchronously process command and send response
	go r.executeCommandAndSendResponse(ctx, event, command, refactionID)

	return nil
}

// executeCommandAndSendResponse executes the command and sends a response
func (r *rootHandler) executeCommandAndSendResponse(ctx context.Context, event *larkim.P2MessageReceiveV1, command *Command, refactionID string) {
	defer r.deleteReaction(*event.Event.Message.MessageId, refactionID)

	message, err := r.handleCommand(ctx, command)
	if err == nil {
		r.sendResponse(*event.Event.Message.MessageId, StatusSuccess, message)
		return
	}

	// Send different levels of response based on error type
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
}

// parsePrivateCommand parses commands from private messages
func (r *rootHandler) parsePrivateCommand(event *larkim.P2MessageReceiveV1) *Command {
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

// parseGroupCommand parses commands from group messages
// Supported format: @bot /command arg1 arg2...
func (r *rootHandler) parseGroupCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)
	r.logger.Debug().Str("messageContent", messageContent).Msg("parse group command")

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		return nil
	}

	// In Lark messages, mentions are converted to @_user_X format
	// Use regex to match commands after @_user_X: /command arg1 arg2...
	re := regexp.MustCompile(`@_user_\d+\s+(/\S+)(.*)`)
	matches := re.FindStringSubmatch(content.Text)

	if len(matches) < 3 {
		r.logger.Debug().Str("content", content.Text).Msg("no command match found")
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
