package handler

import (
	"strings"
	"testing"

	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
)

func TestExtractTextFromMessage_TextMessage(t *testing.T) {
	// Test plain text message
	messageContent := `{"text":"@_user_1 /ask what is TiDB?"}`
	messageType := "text"

	text, err := extractTextFromMessage(messageContent, &messageType)
	if err != nil {
		t.Errorf("Expected no error, got: %v", err)
	}

	expected := "@_user_1 /ask what is TiDB?"
	if text != expected {
		t.Errorf("Expected text '%s', got '%s'", expected, text)
	}
}

func TestExtractTextFromMessage_PostMessage(t *testing.T) {
	// Test rich text (post) message
	messageContent := `{
		"zh_cn": {
			"content": [
				[
					{"tag": "at", "user_id": "ou_xxx"},
					{"tag": "text", "text": " /ask Recently for one branch"}
				]
			]
		}
	}`
	messageType := "post"

	text, err := extractTextFromMessage(messageContent, &messageType)
	if err != nil {
		t.Errorf("Expected no error, got: %v", err)
	}

	// Should contain the @mention placeholder and the command
	if text == "" {
		t.Errorf("Expected non-empty text, got empty string")
	}

	// Check that it contains the command
	if !strings.Contains(text, "/ask") {
		t.Errorf("Expected text to contain '/ask', got '%s'", text)
	}
}

func TestExtractTextFromMessage_PostMessageEnglish(t *testing.T) {
	// Test rich text (post) message with English content
	messageContent := `{
		"en_us": {
			"content": [
				[
					{"tag": "at", "user_id": "ou_xxx"},
					{"tag": "text", "text": " /help"}
				]
			]
		}
	}`
	messageType := "post"

	text, err := extractTextFromMessage(messageContent, &messageType)
	if err != nil {
		t.Errorf("Expected no error, got: %v", err)
	}

	// Check that it contains the command
	if !strings.Contains(text, "/help") {
		t.Errorf("Expected text to contain '/help', got '%s'", text)
	}
}

func TestParseGroupCommand_TextMessage(t *testing.T) {
	// Test parsing group command from text message
	content := `{"text":"@_user_1 /ask what is TiDB?"}`
	messageType := "text"

	event := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:     &content,
				MessageType: &messageType,
			},
		},
	}

	cmd := parseGroupCommand(event)
	if cmd == nil {
		t.Fatal("Expected command, got nil")
	}

	if cmd.Name != "/ask" {
		t.Errorf("Expected command '/ask', got '%s'", cmd.Name)
	}

	if len(cmd.Args) != 3 {
		t.Errorf("Expected 3 args, got %d", len(cmd.Args))
	}
}

func TestParseGroupCommand_PostMessage(t *testing.T) {
	// Test parsing group command from rich text (post) message
	content := `{
		"zh_cn": {
			"content": [
				[
					{"tag": "at", "user_id": "ou_xxx"},
					{"tag": "text", "text": " /ask what is TiDB?"}
				]
			]
		}
	}`
	messageType := "post"

	event := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:     &content,
				MessageType: &messageType,
			},
		},
	}

	cmd := parseGroupCommand(event)
	if cmd == nil {
		t.Fatal("Expected command, got nil")
	}

	if cmd.Name != "/ask" {
		t.Errorf("Expected command '/ask', got '%s'", cmd.Name)
	}
}

func TestParsePrivateCommand_TextMessage(t *testing.T) {
	// Test parsing private command from text message
	content := `{"text":"/help"}`
	messageType := "text"

	event := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:     &content,
				MessageType: &messageType,
			},
		},
	}

	cmd := parsePrivateCommand(event)
	if cmd == nil {
		t.Fatal("Expected command, got nil")
	}

	if cmd.Name != "/help" {
		t.Errorf("Expected command '/help', got '%s'", cmd.Name)
	}
}

func TestParsePrivateCommand_PostMessage(t *testing.T) {
	// Test parsing private command from rich text (post) message
	content := `{
		"en_us": {
			"content": [
				[
					{"tag": "text", "text": "/ask what is TiDB?"}
				]
			]
		}
	}`
	messageType := "post"

	event := &larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:     &content,
				MessageType: &messageType,
			},
		},
	}

	cmd := parsePrivateCommand(event)
	if cmd == nil {
		t.Fatal("Expected command, got nil")
	}

	if cmd.Name != "/ask" {
		t.Errorf("Expected command '/ask', got '%s'", cmd.Name)
	}

	if len(cmd.Args) != 3 {
		t.Errorf("Expected 3 args, got %d", len(cmd.Args))
	}
}
