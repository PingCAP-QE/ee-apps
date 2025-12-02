package handler

import (
	"encoding/json"
	"regexp"
	"slices"
	"strings"

	larkcontact "github.com/larksuite/oapi-sdk-go/v3/service/contact/v3"
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
	if len(event.Event.Message.Mentions) == 0 {
		return false
	}

	for _, mention := range event.Event.Message.Mentions {
		mentioned := mention != nil && mention.Id != nil && mention.Id.OpenId != nil && *mention.Id.OpenId == botOpenID
		if mentioned {
			log.Debug().Interface("mention", mention).Msg("I am mentioned")
			return true
		}
	}
	return false
}

// extractTextFromMessage extracts text from either plain text or rich text (post) messages
func extractTextFromMessage(messageContent string, messageType *string) (string, error) {
	// Default to text if messageType is not specified
	if messageType == nil || *messageType == "text" {
		var content commandLarkMsgContent
		if err := json.Unmarshal([]byte(messageContent), &content); err != nil {
			log.Error().Err(err).Msg("Failed to unmarshal text message content")
			return "", err
		}
		return content.Text, nil
	}

	// Handle rich text (post) messages
	if *messageType == "post" {
		var post postContent
		if err := json.Unmarshal([]byte(messageContent), &post); err != nil {
			log.Error().Err(err).Msg("Failed to unmarshal post message content")
			return "", err
		}

		// Extract text from the first available language version
		var lang *postLanguage
		if post.EnUs != nil {
			lang = post.EnUs
		} else if post.ZhCn != nil {
			lang = post.ZhCn
		} else if post.JaJp != nil {
			lang = post.JaJp
		}

		if lang == nil {
			log.Warn().Msg("No language content found in post message")
			return "", nil
		}

		// Concatenate all text elements
		var textParts []string
		for _, line := range lang.Content {
			for _, elem := range line {
				if elem.Tag == "text" {
					textParts = append(textParts, elem.Text)
				} else if elem.Tag == "at" {
					// Preserve @mentions in the format @_user_X
					textParts = append(textParts, "@_user_1")
				}
			}
			// Add newline after each line of content
			if len(line) > 0 {
				textParts = append(textParts, " ")
			}
		}

		return strings.TrimSpace(strings.Join(textParts, "")), nil
	}

	log.Warn().Str("messageType", *messageType).Msg("Unsupported message type")
	return "", nil
}

// parseGroupCommand parses commands from group messages
// Supported format: @bot /command arg1 arg2...
func parseGroupCommand(event *larkim.P2MessageReceiveV1) *Command {
	messageContent := strings.TrimSpace(*event.Event.Message.Content)

	// Extract text from either text or post messages
	text, err := extractTextFromMessage(messageContent, event.Event.Message.MessageType)
	if err != nil {
		log.Error().Err(err).Msg("Failed to extract text from message")
		return nil
	}

	if text == "" {
		log.Debug().Msg("Empty message text after extraction")
		return nil
	}

	// In Lark messages, mentions are converted to @_user_X format
	// Use regex to match commands after @_user_X: /command arg1 arg2...
	re := regexp.MustCompile(`@_user_\d+\s+(/\S+)(.*)`)
	matches := re.FindStringSubmatch(text)

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

	// Extract text from either text or post messages
	text, err := extractTextFromMessage(messageContent, event.Event.Message.MessageType)
	if err != nil {
		log.Error().Err(err).Msg("Failed to extract text from message")
		return nil
	}

	if text == "" {
		log.Debug().Msg("Empty message text after extraction")
		return nil
	}

	messageParts := strings.Fields(text)
	if len(messageParts) < 1 {
		return nil
	}

	return &Command{Name: messageParts[0], Args: messageParts[1:]}
}

func shouldHandle(event *larkim.P2MessageReceiveV1, botOpenID string) *Command {
	// Log message type for debugging
	msgTypeStr := "unknown"
	if event.Event.Message.MessageType != nil {
		msgTypeStr = *event.Event.Message.MessageType
	}
	log.Debug().Str("messageType", msgTypeStr).Msg("Processing message")

	// Determine message type and whether the bot was mentioned
	msgType, _, isMentionBot := determineMessageType(event, botOpenID)

	if msgType == msgTypeGroup && isMentionBot {
		return parseGroupCommand(event)
	} else if msgType == msgTypePrivate {
		return parsePrivateCommand(event)
	}
	return nil
}

func parseUserCustomAttr(attrID string, info *larkcontact.User) *string {
	emptyValues := []string{"无", "NaN", "N/A", "NA", ""}

	for _, attr := range info.CustomAttrs {
		if attr.Id != nil && *attr.Id == attrID && attr.Value != nil && attr.Value.Text != nil {
			if slices.Contains(emptyValues, *attr.Value.Text) {
				return nil
			}
			return attr.Value.Text
		}
	}

	return nil
}
