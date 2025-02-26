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

	// 消息类型
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
	MsgType string // 消息类型：private 或 group
	ChatID  string // 群组ID，仅当MsgType为group时有效
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
	botName    string // 机器人名称，用于匹配群聊中的@消息
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

	// 获取机器人名称，用于匹配群聊中的@消息
	botName := ""
	if botNameVal, ok := cfg["bot_name"]; ok {
		if botNameStr, ok := botNameVal.(string); ok {
			botName = botNameStr
		}
	}
	if botName == "" {
		botName = "机器人" // 默认名称
	}

	h := &rootHandler{Client: respondCli, Config: cfg, eventCache: cache, botName: botName}
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

	// 判断消息类型（私聊或群聊）
	var msgType string
	var chatID string

	// 检查消息是否来自群聊
	if event.Event.Message.ChatId != nil && event.Event.Message.ChatType != nil && *event.Event.Message.ChatType == "group" {
		msgType = msgTypeGroup
		chatID = *event.Event.Message.ChatId
		hl.Debug().Str("chatID", chatID).Msg("received message from group chat")
	} else {
		msgType = msgTypePrivate
		hl.Debug().Msg("received message from private chat")
	}

	// 根据消息类型解析命令
	var command *Command
	if msgType == msgTypeGroup {
		command = r.parseGroupCommand(event)
	} else {
		command = r.parsePrivateCommand(event)
	}

	if command == nil {
		hl.Debug().Msg("none command received")
		return nil
	}

	// 设置命令的消息类型和群组ID
	command.MsgType = msgType
	command.ChatID = chatID

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

// parsePrivateCommand 解析私聊消息中的命令
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

// parseGroupCommand 解析群聊消息中的命令
// 支持格式：@机器人 /command arg1 arg2...
func (r *rootHandler) parseGroupCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)
	hl := log.With().Str("eventId", *event.Event.Message.MessageId).Logger()
	hl.Debug().Str("messageContent", messageContent).Msg("parse group command")

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		return nil
	}

	// 匹配 @机器人 后面的命令
	// 使用正则表达式匹配 @机器人 后面的内容
	re := regexp.MustCompile(`@` + r.botName + `\s+(/\S+)(.*)`)
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

	// 检查是否是特权命令
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
