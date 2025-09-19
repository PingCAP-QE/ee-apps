package handler

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/allegro/bigcache/v3"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	larkcontact "github.com/larksuite/oapi-sdk-go/v3/service/contact/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/audit"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/botinfo"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/response"
)

func NewRootForMessage(cfg *config.Config, clientOpts ...lark.ClientOptionFunc) func(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	h := &rootHandler{Config: *cfg}
	return h.Handle
}

type rootHandler struct {
	*lark.Client

	Config config.Config

	botOpenID       string
	commandRegistry map[string]CommandConfig
	eventCache      *bigcache.BigCache
	logger          zerolog.Logger

	initOnce sync.Once
	initErr  error
}

func (r *rootHandler) Handle(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	// Ensure initialization is done only once
	r.initOnce.Do(func() {
		r.initErr = r.initialize()
	})

	// If initialization failed, return the error
	if r.initErr != nil {
		return r.initErr
	}

	eventID := event.EventV2Base.Header.EventID
	messageID := *event.Event.Message.MessageId

	chatType := "unknown"
	if event.Event.Message.ChatType != nil {
		chatType = *event.Event.Message.ChatType
	}

	if chatType == "p2p" && event.Event.Message.Mentions != nil {
		for _, mention := range event.Event.Message.Mentions {
			if mention.Id != nil && mention.Id.OpenId != nil && *mention.Id.OpenId == r.botOpenID {
				log.Info().Str("eventId", eventID).Msg("Bot mentioned in P2P chat, sending reminder.")
				hintMessage := "Hi there! In private chats, you don't need to @ me. Just type your command directly (e.g., `/help`). You only need to @ mention me in group chats."
				_ = r.sendResponse(messageID, false, StatusInfo, hintMessage)
				// Stop processing this event further, because it's a private chat.
				return nil
			}
		}
	}

	msgType := chatType

	hl := r.logger.With().
		Str("eventId", eventID).
		Str("msgType", msgType).
		Logger()

	// check if the event has been handled.
	_, err := r.eventCache.Get(eventID)
	switch err {
	case nil:
		return nil
	case bigcache.ErrEntryNotFound:
		r.eventCache.Set(eventID, []byte(eventID))
	default:
		hl.Err(err).Msg("Unexpected error accessing event cache")
		return err
	}

	command := shouldHandle(event, r.botOpenID)
	if command == nil {
		return nil
	}

	refactionID, err := r.addReaction(messageID)
	if err != nil {
		hl.Err(err).Msg("send heartbeat failed")
		return err
	}

	senderOpenID := *event.Event.Sender.SenderId.OpenId
	res, err := r.getUserInfo(ctx, *event.Event.Sender.SenderId.OpenId)
	if err != nil {
		hl.Err(err).Msg("get user info failed.")
		return err
	}

	command.Sender = &CommandActor{OpenID: senderOpenID, Email: *res.Data.User.Email}
	if r.Config.UserCustomAttrIDs != nil && r.Config.UserCustomAttrIDs.GitHubID != "" {
		command.Sender.GitHubID = parseUserCustomAttr(r.Config.UserCustomAttrIDs.GitHubID, res.Data.User)
	}

	go func() {
		defer r.deleteReaction(messageID, refactionID)
		asyncLog := hl.With().Str("command", command.Name).
			Any("args", command.Args).
			Str("sender", *event.Event.Sender.SenderId.OpenId).
			Logger()

		asyncLog.Info().Msg("Processing command")
		message, err := r.handleCommand(ctx, command)
		replyInThread := (chatType == "group")
		r.feedbackCommandResult(messageID, replyInThread, message, err, asyncLog)
	}()

	return nil
}

func (r *rootHandler) getUserInfo(ctx context.Context, senderOpenID string) (*larkcontact.GetUserResp, error) {
	res, err := r.Contact.User.Get(ctx, larkcontact.NewGetUserReqBuilder().
		UserIdType(larkcontact.UserIdTypeOpenId).
		UserId(senderOpenID).
		Build())
	if err != nil {
		return nil, err
	}
	if !res.Success() {
		return nil, res.CodeError
	}

	return res, nil
}

func (r *rootHandler) initialize() error {
	// initialize logger
	r.logger = log.With().Str("component", "rootHandler").Logger()

	// initialize event cache
	cacheCfg := bigcache.DefaultConfig(10 * time.Minute)
	cacheCfg.Logger = &r.logger
	r.eventCache, _ = bigcache.New(context.Background(), cacheCfg)

	// initialize Lark client
	producerOpts := []lark.ClientOptionFunc{}
	if r.Config.Debug {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelDebug), lark.WithLogReqAtDebug(true))
	} else {
		producerOpts = append(producerOpts, lark.WithLogLevel(larkcore.LogLevelInfo))
	}
	r.Client = lark.NewClient(r.Config.AppID, r.Config.AppSecret, producerOpts...)

	// fetch and set the bot OpenID using the botinfo package.
	ctxWithTimeout, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	openID, err := botinfo.GetBotOpenID(ctxWithTimeout, r.Config.AppID, r.Config.AppSecret)
	if err != nil {
		r.logger.Err(err).Msg("failed to get bot openID from botinfo package")
		return err
	}

	r.botOpenID = openID
	r.logger.Info().Str("botOpenID", r.botOpenID).Msg("Bot OpenID initialized via botinfo package")

	r.commandRegistry = map[string]CommandConfig{
		"/cherry-pick-invite": {
			Description:  "Grant a collaborator permission to edit a cherry-pick PR",
			Handler:      runCommandCherryPickInvite,
			AuditWebhook: r.Config.CherryPickInvite.AuditWebhook,
			SetupContext: setupCtxCherryPickInvite,
		},
		"/devbuild": {
			Description:  "Trigger a devbuild or check build status",
			Handler:      runCommandDevbuild,
			AuditWebhook: r.Config.DevBuild.AuditWebhook,
			SetupContext: setupCtxDevbuild,
		},
		"/ask": {
			Description:  "Ask a question with LLM",
			Handler:      runCommandAsk,
			AuditWebhook: r.Config.Ask.AuditWebhook,
			SetupContext: setupAskCtx,
		},
	}

	return nil
}

func (r *rootHandler) feedbackCommandResult(messageID string, replyInThread bool, message string, err error, asyncLog zerolog.Logger) {
	if err == nil {
		asyncLog.Info().Msg("Command processed successfully")
		r.sendResponse(messageID, replyInThread, StatusSuccess, message)
		return
	}

	// send different level response for the error types.
	switch e := err.(type) {
	case *SkipError:
		asyncLog.Info().Msg("Command was skipped")
		responseMsg := strings.Join([]string{message, e.Error()}, "\n---\n")
		r.sendResponse(messageID, replyInThread, StatusSkip, responseMsg)
	case *InformationError:
		asyncLog.Info().Msg("Command was handled but just feedback information")
		responseMsg := strings.Join([]string{message, e.Error()}, "\n---\n")
		r.sendResponse(messageID, replyInThread, StatusInfo, responseMsg)
	default:
		asyncLog.Err(err).Msg("Command processing failed")
		responseMsg := strings.Join([]string{message, e.Error()}, "\n---\n")
		r.sendResponse(messageID, replyInThread, StatusFailure, responseMsg)
	}
}

func (r *rootHandler) handleCommand(ctx context.Context, command *Command) (string, error) {
	cmdLogger := r.logger.With().
		Str("command", command.Name).
		Str("sender", command.Sender.Email).
		Logger()

	cmdConfig, err := r.getCommandConfig(command, cmdLogger)
	if err != nil {
		return "", err
	}

	runCtx := cmdConfig.SetupContext(ctx, r.Config, command.Sender)
	result, err := cmdConfig.Handler(runCtx, command.Args)
	if err != nil {
		return result, err
	}

	if cmdConfig.AuditWebhook != "" {
		if auditErr := r.audit(cmdConfig.AuditWebhook, command); auditErr != nil {
			cmdLogger.Warn().Err(auditErr).Msg("Failed to audit command")
		}
	}

	return result, nil
}

func (r *rootHandler) getCommandConfig(command *Command, cmdLogger zerolog.Logger) (*CommandConfig, error) {
	cmdConfig, ok := r.commandRegistry[command.Name]
	if !ok {
		cmdLogger.Warn().Msg("Unsupported command")

		return nil, NewSkipError(fmt.Sprintf("Unsupported command: %s\n\n%s", command.Name, r.availableCommandsHelp()))
	}
	return &cmdConfig, nil
}

func (r *rootHandler) audit(auditWebhook string, command *Command) error {
	auditInfo := &audit.AuditInfo{
		UserEmail: command.Sender.Email,
		Command:   command.Name,
		Args:      command.Args,
	}

	return audit.RecordAuditMessage(auditInfo, auditWebhook)
}

func (r *rootHandler) sendResponse(reqMsgID string, replyInThread bool, status, message string) error {
	replyMsg, err := response.NewReplyMessageReq(reqMsgID, replyInThread, status, message)
	if err != nil {
		log.Err(err).Msg("Failed to build reply message request.")
		return err
	}

	res, err := r.Im.Message.Reply(context.Background(), replyMsg)
	if err != nil {
		log.Err(err).Msg("Failed to send reply message.")
		return err
	}

	if !res.Success() {
		log.Error().Str("code", fmt.Sprintf("%d", res.Code)).Str("msg", res.Msg).Msg("Failed to send reply, API error.")
		return fmt.Errorf("Lark API error sending reply: %s (code: %d)", res.Msg, res.Code)
	}

	log.Info().Bool("repliedInThread", replyInThread).Msg("Reply message sent successfully.")
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

func (r *rootHandler) availableCommandsHelp() string {
	// Initialize the help text for available commands
	commandsList := make([]string, 0, len(r.commandRegistry))
	for k := range r.commandRegistry {
		commandsList = append(commandsList, k)
	}
	slices.Sort(commandsList)

	// Build the help text that will be reused
	helpTexts := []string{"Available commands:", ""}
	for _, cmd := range commandsList {
		helpTexts = append(helpTexts, strings.Join([]string{fmt.Sprintf("• %s", cmd), r.commandRegistry[cmd].Description}, " - "))
	}
	helpTexts = append(helpTexts, "", "Use [command] --help for more information")

	return strings.Join(helpTexts, "\n")
}
