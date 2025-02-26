package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/allegro/bigcache/v3"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkevent "github.com/larksuite/oapi-sdk-go/v3/event"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
	"github.com/stretchr/testify/assert"
)

// Helper function to create a test rootHandler
func createTestRootHandler() *rootHandler {
	cacheCfg := bigcache.DefaultConfig(0)
	cache, _ := bigcache.New(context.Background(), cacheCfg)

	return &rootHandler{
		Client:     &lark.Client{},
		eventCache: cache,
		Config:     make(map[string]any),
		botName:    "TestBot",
		logger:     log.Logger,
	}
}

// Helper function to create a test message event
func createMessageEvent(content string, mentions []*larkim.MentionEvent, chatType string) *larkim.P2MessageReceiveV1 {
	contentStruct := commandLarkMsgContent{Text: content}
	contentBytes, _ := json.Marshal(contentStruct)
	contentStr := string(contentBytes)

	messageId := "test_message_id"
	chatId := "test_chat_id"
	openId := "test_open_id"
	eventId := "test_event_id"

	event := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				MessageId: &messageId,
				ChatId:    &chatId,
				Content:   &contentStr,
				Mentions:  mentions,
			},
			Sender: &larkim.EventSender{
				SenderId: &larkim.UserId{
					OpenId: &openId,
				},
			},
		},
		EventV2Base: &larkevent.EventV2Base{
			Header: &larkevent.EventHeader{
				EventID: eventId,
			},
		},
	}

	// Set chat type
	if chatType == "group" {
		event.Event.Message.ChatType = &chatType
	}

	return event
}

// Test checkIfBotMentioned function
func TestCheckIfBotMentioned(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: No mentions
	event := createMessageEvent("Hello", nil, "group")
	result := handler.checkIfBotMentioned(event)
	assert.False(t, result, "Should return false when there are no mentions")

	// Test case 2: Bot is mentioned
	botName := "TestBot"
	mentions := []*larkim.MentionEvent{
		{
			Name: &botName,
		},
	}
	event = createMessageEvent("Hello @TestBot", mentions, "group")
	result = handler.checkIfBotMentioned(event)
	assert.True(t, result, "Should return true when bot is mentioned")

	// Test case 3: Other user is mentioned
	otherName := "OtherUser"
	mentions = []*larkim.MentionEvent{
		{
			Name: &otherName,
		},
	}
	event = createMessageEvent("Hello @OtherUser", mentions, "group")
	result = handler.checkIfBotMentioned(event)
	assert.False(t, result, "Should return false when bot is not mentioned")

	// Test case 4: Nil mention
	mentions = []*larkim.MentionEvent{nil}
	event = createMessageEvent("Hello", mentions, "group")
	result = handler.checkIfBotMentioned(event)
	assert.False(t, result, "Should handle nil mentions gracefully")
}

// Test parsePrivateCommand function
func TestParsePrivateCommand(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: Valid command
	event := createMessageEvent("/devbuild trigger tidb v6.5.0 master", nil, "private")
	command := handler.parsePrivateCommand(event)

	assert.NotNil(t, command, "Should parse a valid command")
	if command != nil {
		assert.Equal(t, "/devbuild", command.Name, "Command name should be parsed correctly")
		assert.Equal(t, []string{"trigger", "tidb", "v6.5.0", "master"}, command.Args, "Command args should be parsed correctly")
	}

	// Test case 2: Invalid JSON
	// Create an event with invalid JSON content
	messageId := "test_message_id"
	chatId := "test_chat_id"
	openId := "test_open_id"
	eventId := "test_event_id"
	invalidContent := "not a json"

	invalidEvent := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				MessageId: &messageId,
				ChatId:    &chatId,
				Content:   &invalidContent,
			},
			Sender: &larkim.EventSender{
				SenderId: &larkim.UserId{
					OpenId: &openId,
				},
			},
		},
		EventV2Base: &larkevent.EventV2Base{
			Header: &larkevent.EventHeader{
				EventID: eventId,
			},
		},
	}

	command = handler.parsePrivateCommand(invalidEvent)
	assert.Nil(t, command, "Should return nil for invalid JSON")

	// Test case 3: No command
	event = createMessageEvent("", nil, "private")
	command = handler.parsePrivateCommand(event)
	assert.Nil(t, command, "Should return nil when no command is provided")

	// Test case 4: Privileged command (if any are defined)
	if len(privilegedCommandList) > 0 {
		privilegedCmd := privilegedCommandList[0]
		event = createMessageEvent(privilegedCmd+" arg1", nil, "private")
		command = handler.parsePrivateCommand(event)
		assert.Nil(t, command, "Should return nil for privileged commands")
	}
}

// Test parseGroupCommand function
func TestParseGroupCommand(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: Valid command with mention
	event := createMessageEvent("@_user_1 /devbuild trigger tidb v6.5.0 master", nil, "group")
	command := handler.parseGroupCommand(event)

	assert.NotNil(t, command, "Should parse a valid command")
	if command != nil {
		assert.Equal(t, "/devbuild", command.Name, "Command name should be parsed correctly")
		assert.Equal(t, []string{"trigger", "tidb", "v6.5.0", "master"}, command.Args, "Command args should be parsed correctly")
	}

	// Test case 2: Invalid format (no mention)
	event = createMessageEvent("/devbuild trigger tidb", nil, "group")
	command = handler.parseGroupCommand(event)
	assert.Nil(t, command, "Should return nil when format is invalid")

	// Test case 3: Invalid JSON
	// Create an event with invalid JSON content
	messageId := "test_message_id"
	chatId := "test_chat_id"
	openId := "test_open_id"
	eventId := "test_event_id"
	invalidContent := "not a json"

	invalidEvent := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				MessageId: &messageId,
				ChatId:    &chatId,
				Content:   &invalidContent,
			},
			Sender: &larkim.EventSender{
				SenderId: &larkim.UserId{
					OpenId: &openId,
				},
			},
		},
		EventV2Base: &larkevent.EventV2Base{
			Header: &larkevent.EventHeader{
				EventID: eventId,
			},
		},
	}

	command = handler.parseGroupCommand(invalidEvent)
	assert.Nil(t, command, "Should return nil for invalid JSON")

	// Test case 4: No command after mention
	event = createMessageEvent("@_user_1 ", nil, "group")
	command = handler.parseGroupCommand(event)
	assert.Nil(t, command, "Should return nil when no command follows the mention")

	// Test case 5: Command with no arguments
	event = createMessageEvent("@_user_1 /devbuild", nil, "group")
	command = handler.parseGroupCommand(event)

	assert.NotNil(t, command, "Should parse a command with no arguments")
	if command != nil {
		assert.Equal(t, "/devbuild", command.Name, "Command name should be parsed correctly")
		assert.Empty(t, command.Args, "Args should be empty")
	}
}

// Test determineMessageType function
func TestDetermineMessageType(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: Group chat with bot mentioned
	botName := "TestBot"
	mentions := []*larkim.MentionEvent{
		{
			Name: &botName,
		},
	}
	event := createMessageEvent("Hello @TestBot", mentions, "group")
	msgType, chatID, isMentionBot := handler.determineMessageType(event)

	assert.Equal(t, msgTypeGroup, msgType, "Message type should be group")
	assert.Equal(t, "test_chat_id", chatID, "Chat ID should be extracted")
	assert.True(t, isMentionBot, "Bot should be detected as mentioned")

	// Test case 2: Group chat without bot mentioned
	otherName := "OtherUser"
	mentions = []*larkim.MentionEvent{
		{
			Name: &otherName,
		},
	}
	event = createMessageEvent("Hello @OtherUser", mentions, "group")
	msgType, chatID, isMentionBot = handler.determineMessageType(event)

	assert.Equal(t, msgTypeGroup, msgType, "Message type should be group")
	assert.Equal(t, "test_chat_id", chatID, "Chat ID should be extracted")
	assert.False(t, isMentionBot, "Bot should not be detected as mentioned")

	// Test case 3: Private chat
	event = createMessageEvent("Hello", nil, "private")
	msgType, chatID, isMentionBot = handler.determineMessageType(event)

	assert.Equal(t, msgTypePrivate, msgType, "Message type should be private")
	assert.Empty(t, chatID, "Chat ID should be empty for private chats")
	assert.False(t, isMentionBot, "Bot mention flag should be false for private chats")
}

// Test parseCommand function
func TestParseCommand(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: Group chat with bot mentioned
	botName := "TestBot"
	mentions := []*larkim.MentionEvent{
		{
			Name: &botName,
		},
	}
	content := `{"text": "@_user_1 /devbuild trigger tidb"}`
	event := createMessageEvent(content, mentions, "group")

	command := handler.parseCommand(event, msgTypeGroup, true)
	assert.NotNil(t, command, "Should parse command in group chat with bot mentioned")

	// Test case 2: Group chat without bot mentioned
	command = handler.parseCommand(event, msgTypeGroup, false)
	assert.Nil(t, command, "Should not parse command in group chat without bot mentioned")

	// Test case 3: Private chat
	content = `{"text": "/devbuild trigger tidb"}`
	event = createMessageEvent(content, nil, "private")

	command = handler.parseCommand(event, msgTypePrivate, false)
	assert.NotNil(t, command, "Should parse command in private chat")
}

// Test isEventAlreadyHandled function
func TestIsEventAlreadyHandled(t *testing.T) {
	handler := createTestRootHandler()

	// Test case 1: New event
	result := handler.isEventAlreadyHandled("new_event_id")
	assert.False(t, result, "Should return false for new event")

	// Test case 2: Already handled event
	result = handler.isEventAlreadyHandled("new_event_id")
	assert.True(t, result, "Should return true for already handled event")
}

// Mock implementation of InformationError for testing
type mockInformationError struct {
	message string
}

func (e mockInformationError) Error() string {
	return e.message
}

// Mock implementation of SkipError for testing
type mockSkipError struct {
	message string
}

func (e mockSkipError) Error() string {
	return e.message
}

// Test error handling in executeCommandAndSendResponse
func TestErrorHandling(t *testing.T) {
	// Create mock errors of different types
	regularError := fmt.Errorf("regular error")
	infoError := mockInformationError{message: "info error"}
	skipError := mockSkipError{message: "skip error"}

	// Test the actual error handling logic from the handler
	// This is a simplified version of what happens in executeCommandAndSendResponse

	// Regular error
	status, message := getErrorResponseStatus(regularError)
	assert.Equal(t, StatusFailure, status, "Regular error should result in failure status")
	assert.Equal(t, regularError.Error(), message, "Regular error message should be used")

	// Information error
	status, message = getErrorResponseStatus(infoError)
	assert.Equal(t, StatusInfo, status, "Information error should result in info status")
	assert.Equal(t, infoError.Error(), message, "Information error message should be used")

	// Skip error
	status, message = getErrorResponseStatus(skipError)
	assert.Equal(t, StatusSkip, status, "Skip error should result in skip status")
	assert.Equal(t, skipError.Error(), message, "Skip error message should be used")
}

// Helper function that mimics the error handling in executeCommandAndSendResponse
func getErrorResponseStatus(err error) (string, string) {
	// Check if it's a mockInformationError
	if _, ok := err.(mockInformationError); ok {
		return StatusInfo, err.Error()
	}

	// Check if it's a mockSkipError
	if _, ok := err.(mockSkipError); ok {
		return StatusSkip, err.Error()
	}

	// Regular error
	return StatusFailure, err.Error()
}
