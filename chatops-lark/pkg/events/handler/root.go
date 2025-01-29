package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/response"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcontact "github.com/larksuite/oapi-sdk-go/v3/service/contact/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
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
		"/approve_pr",
		"/label_pr",
		"/check_pr_label",
		"/create_milestone",
		"/merge_pr",
		"/create_tag_from_branch",
		"/trigger_job",
		"/create_branch_from_tag",
		"/create_new_branch_from_branch",
		"/build_hotfix",
		"/sync_docker_image",
		"/create_product_release",
		"/create_hotfix_branch",
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

func NewRootForMessage(respondCli *lark.Client) func(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
	return func(ctx context.Context, event *larkim.P2MessageReceiveV1) error {
		return rootForMessage(ctx, event, respondCli)
	}
}

func rootForMessage(ctx context.Context, event *larkim.P2MessageReceiveV1, respondCli *lark.Client) error {
	log.Debug().Msg("rootForMessage")
	if command := shouldHandle(event); command != nil {
		senderOpenID := *event.Event.Sender.SenderId.OpenId
		res, err := respondCli.Contact.User.Get(ctx, larkcontact.NewGetUserReqBuilder().
			UserIdType(larkcontact.UserIdTypeOpenId).
			UserId(senderOpenID).
			Build(),
		)
		if err != nil {
			log.Err(err).Msg("get user info failed.")
			return err
		}

		command.Sender = &CommandSender{OpenID: senderOpenID, Email: *res.Data.User.Email}

		var status string
		message, err := handleCommand(ctx, command)
		if err != nil {
			status = "failed"
			message = fmt.Sprintf("%s\n---\n**error:**\n%v", message, err)
		} else {
			status = "success"
		}

		return sendResponse(*event.Event.Message.MessageId, status, message, respondCli)
	}
	return nil
}

func shouldHandle(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)
	log.Debug().Str("content", messageContent).Msg("msg")

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

func handleCommand(_ context.Context, command *Command) (string, error) {
	switch command.Name {
	case "/devbuild":
		return runCommandDevbuild(command.Args, command.Sender)
	default:
		return "", fmt.Errorf("not support command: %s", command.Name)
	}
}

func sendResponse(reqMsgID string, status, message string, respondCli *lark.Client) error {
	replyMsg, err := response.NewReplyMessageReq(reqMsgID, status, message)
	if err != nil {
		log.Err(err).Msg("render msg faled.")
		return err
	}

	res, err := respondCli.Im.Message.Reply(context.Background(), replyMsg)
	if err != nil {
		log.Err(err).Msg("send msg error.")
		return err
	}

	if res.Success() {
		log.Debug().Msg("message send success")
	} else {
		log.Error().Msg("message send failed")
	}

	return nil
}
