package handler

import (
	"encoding/json"
	"regexp"
	"strings"

	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
)

// determineMessageType determines the message type and checks if the bot was mentioned
func determineMessageType(event *larkim.P2MessageReceiveV1, botOpenID string) (msgType string, chatID string, isMentionBot bool) {
	// Check if the message is from a group chat
	if event.Event.Message.ChatType != nil && *event.Event.Message.ChatType == "group" {
		msgType = msgTypeGroup
		if event.Event.Message.ChatId != nil {
			chatID = *event.Event.Message.ChatId
		}
		isMentionBot = checkIfBotMentioned(event, botOpenID)
	} else {
		msgType = msgTypePrivate
	}

	return msgType, chatID, isMentionBot
}

// checkIfBotMentioned checks if the bot was mentioned in the message using OpenID
func checkIfBotMentioned(event *larkim.P2MessageReceiveV1, botOpenID string) bool {
	if event.Event.Message.Mentions == nil || len(event.Event.Message.Mentions) == 0 {
		return false
	}

	for _, mention := range event.Event.Message.Mentions {
		log.Info().Interface("mention", mention).Msg("mention")
		if mention != nil && mention.Id != nil && mention.Id.OpenId != nil && *mention.Id.OpenId == botOpenID {
			return true
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
		log.Error().Err(err).Msg("Failed to unmarshal message content")
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

	return &Command{Name: commandName, Args: args}
}

// parsePrivateCommand parses commands from private messages
func parsePrivateCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)

	var content commandLarkMsgContent
	if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
		log.Error().Err(err).Msg("Failed to unmarshal message content")
		return nil
	}

	messageParts := strings.Fields(content.Text)
	if len(messageParts) < 1 {
		return nil
	}

	return &Command{Name: messageParts[0], Args: messageParts[1:]}
}

func shouldHandle(event *larkim.P2MessageReceiveV1, botOpenID string) *Command {
	// Determine message type and whether the bot was mentioned
	msgType, _, isMentionBot := determineMessageType(event, botOpenID)

	if msgType == msgTypeGroup && isMentionBot {
		return parseGroupCommand(event)
	} else if msgType == msgTypePrivate {
		return parsePrivateCommand(event)
	}
	return nil
}
