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

	botName         string
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

	msgType := "unknown"
	if event.Event.Message.ChatType != nil {
		msgType = *event.Event.Message.ChatType
	}

	hl := r.logger.With().
		Str("eventId", eventID).
		Str("msgType", msgType).
		Logger()

	// check if the event has been handled.
	_, err := r.eventCache.Get(eventID)
	if err == nil {
		return nil
	} else if err == bigcache.ErrEntryNotFound {
		r.eventCache.Set(eventID, []byte(eventID))
	} else {
		hl.Err(err).Msg("Unexpected error accessing event cache")
		return err
	}

	command := shouldHandle(event, r.botName)
	if command == nil {
		return nil
	}

	messageID := *event.Event.Message.MessageId
	refactionID, err := r.addReaction(messageID)
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
		hl.Error().Msg("get user info failed")
		return nil
	}

	command.Sender = &CommandActor{OpenID: senderOpenID, Email: *res.Data.User.Email}

	go func() {
		defer r.deleteReaction(messageID, refactionID)
		asyncLog := hl.With().Str("command", command.Name).
			Any("args", command.Args).
			Str("sender", *event.Event.Sender.SenderId.OpenId).
			Logger()

		asyncLog.Info().Msg("Processing command")
		message, err := r.handleCommand(ctx, command)
		r.feedbackCommandResult(messageID, message, err, asyncLog)
	}()

	return nil
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

	// fetch and set the bot name.
	ctxWithTimeout, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	name, err := botinfo.GetBotName(ctxWithTimeout, r.Config.AppID, r.Config.AppSecret)
	if err != nil {
		r.logger.Err(err).Msg("failed to get bot name")
		return err
	}
	r.botName = name

	// initialize command registry
	r.commandRegistry = map[string]CommandConfig{
		"/cherry-pick-invite": CommandConfig{
			Description:  "Grant a collaborator permission to edit a cherry-pick PR",
			Handler:      runCommandCherryPickInvite,
			AuditWebhook: r.Config.CherryPickInvite.AuditWebhook,
			SetupContext: setupCtxCherryPickInvite,
		},
		"/devbuild": CommandConfig{
			Description:  "Trigger a devbuild or check build status",
			Handler:      runCommandDevbuild,
			AuditWebhook: r.Config.DevBuild.AuditWebhook,
			SetupContext: setupCtxDevbuild,
		},
		"/ask": CommandConfig{
			Description:  "Ask a question with LLM",
			Handler:      runCommandAsk,
			AuditWebhook: r.Config.Ask.AuditWebhook,
			SetupContext: setupAskCtx,
		},
	}

	return nil
}

func (r *rootHandler) feedbackCommandResult(messageID string, message string, err error, asyncLog zerolog.Logger) {
	if err == nil {
		asyncLog.Info().Msg("Command processed successfully")
		r.sendResponse(messageID, StatusSuccess, message)
		return
	}

	// send different level response for the error types.
	switch e := err.(type) {
	case SkipError:
		asyncLog.Info().Msg("Command was skipped")
		message = fmt.Sprintf("%s\n---\n**skip:**\n%v", message, e)
		r.sendResponse(messageID, StatusSkip, message)
	case InformationError:
		asyncLog.Info().Msg("Command was handled but just feedback information")
		message = fmt.Sprintf("%s\n---\n**information:**\n%v", message, e)
		r.sendResponse(messageID, StatusInfo, message)
	default:
		asyncLog.Err(err).Msg("Command processing failed")
		message = fmt.Sprintf("%s\n---\n**error:**\n%v", message, err)
		r.sendResponse(messageID, StatusFailure, message)
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

		return nil, fmt.Errorf("Unsupported command: %s\n\n%s", command.Name, r.availableCommandsHelp())
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
