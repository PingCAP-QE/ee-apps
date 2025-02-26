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
	"github.com/rs/zerolog"
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
	botName    string // 机器人名称，仅用于文档和日志目的
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

	// 获取机器人名称，用于文档和日志等目的
	// 注意：在飞书API中，@机器人会自动转换为@_user_X格式，不需要使用实际名称匹配
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
	r.logger = log.With().Str("eventId", eventID).Logger()

	// 检查事件是否已处理
	if r.isEventAlreadyHandled(eventID) {
		return nil
	}

	// 确定消息类型和是否提及机器人
	msgType, chatID, isMentionBot := r.determineMessageType(event)

	// 解析命令
	command := r.parseCommand(event, msgType, isMentionBot)
	if command == nil {
		r.logger.Debug().Msg("none command received")
		return nil
	}

	// 设置命令的消息类型和群组ID
	command.MsgType = msgType
	command.ChatID = chatID

	// 获取发送者信息并处理命令
	return r.processCommand(ctx, event, command)
}

// isEventAlreadyHandled 检查事件是否已经处理过
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

// determineMessageType 确定消息类型并检查是否提及机器人
func (r *rootHandler) determineMessageType(event *larkim.P2MessageReceiveV1) (msgType string, chatID string, isMentionBot bool) {
	// 检查消息是否来自群聊
	if event.Event.Message.ChatId != nil && event.Event.Message.ChatType != nil && *event.Event.Message.ChatType == "group" {
		msgType = msgTypeGroup
		chatID = *event.Event.Message.ChatId
		isMentionBot = r.checkIfBotMentioned(event)

		// 打印 mention bot 信息
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

// checkIfBotMentioned 检查消息中是否提及了机器人
func (r *rootHandler) checkIfBotMentioned(event *larkim.P2MessageReceiveV1) bool {
	isMentionBot := false

	// 打印 mentions 信息
	if event.Event.Message.Mentions != nil && len(event.Event.Message.Mentions) > 0 {
		for i, mention := range event.Event.Message.Mentions {
			if mention != nil {
				mentionLog := r.logger.Debug().Int("index", i)
				if mention.Name != nil {
					mentionLog = mentionLog.Str("name", *mention.Name)
					if *mention.Name == r.botName {
						isMentionBot = true
					}
				}
				if mention.Key != nil {
					mentionLog = mentionLog.Str("key", *mention.Key)
				}
				if mention.Id != nil {
					mentionLog = mentionLog.Interface("id", mention.Id)
				}
				if mention.TenantKey != nil {
					mentionLog = mentionLog.Str("tenantKey", *mention.TenantKey)
				}
				mentionLog.Msg("mention info")
			}
		}
	} else {
		r.logger.Debug().Msg("no mentions or not mention the bot in the message")
	}

	return isMentionBot
}

// parseCommand 根据消息类型解析命令
func (r *rootHandler) parseCommand(event *larkim.P2MessageReceiveV1, msgType string, isMentionBot bool) *Command {
	if msgType == msgTypeGroup && isMentionBot {
		return r.parseGroupCommand(event)
	} else if msgType == msgTypePrivate {
		return r.parsePrivateCommand(event)
	}
	return nil
}

// processCommand 处理命令并发送响应
func (r *rootHandler) processCommand(ctx context.Context, event *larkim.P2MessageReceiveV1, command *Command) error {
	r.logger.Debug().Str("command", command.Name).Any("args", command.Args).Msg("received command")

	// 添加反应表情
	refactionID, err := r.addReaction(*event.Event.Message.MessageId)
	if err != nil {
		r.logger.Err(err).Msg("send heartbeat reaction failed")
		return err
	}

	// 获取发送者信息
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

	// 异步处理命令并发送响应
	go r.executeCommandAndSendResponse(ctx, event, command, refactionID)

	return nil
}

// executeCommandAndSendResponse 执行命令并发送响应
func (r *rootHandler) executeCommandAndSendResponse(ctx context.Context, event *larkim.P2MessageReceiveV1, command *Command, refactionID string) {
	defer r.deleteReaction(*event.Event.Message.MessageId, refactionID)

	message, err := r.handleCommand(ctx, command)
	if err == nil {
		r.sendResponse(*event.Event.Message.MessageId, StatusSuccess, message)
		return
	}

	// 根据错误类型发送不同级别的响应
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
	r.logger.Debug().Str("messageContent", messageContent).Msg("parse group command")

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		return nil
	}

	// 在飞书消息中，提及会被转换为 @_user_X 格式
	// 使用正则表达式匹配 @_user_X 后面的命令: /command arg1 arg2...
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
