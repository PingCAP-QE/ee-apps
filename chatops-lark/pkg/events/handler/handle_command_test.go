package handler

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
)

// Test handleCommand function
func TestHandleCommand(t *testing.T) {
	handler := createTestRootHandler()
	// Add GitHub token to config
	handler.Config["cherry-pick-invite.github_token"] = "fake-token"

	ctx := context.Background()

	// Test case 1: Unsupported command
	command := &Command{
		Name: "/unsupported",
		Args: []string{},
	}
	_, err := handler.handleCommand(ctx, command)
	assert.Error(t, err, "Should return error for unsupported command")
	assert.Contains(t, err.Error(), "not support command", "Error message should indicate unsupported command")

	// Test case 2: devbuild command
	// This would require mocking the runCommandDevbuild function or using a real implementation
	// with controlled inputs
	command = &Command{
		Name: "/devbuild",
		Args: []string{"-h"},
		Sender: &CommandSender{
			Email: "test@example.com",
		},
	}
	message, err := handler.handleCommand(ctx, command)
	assert.NoError(t, err, "Should not return error for help command")
	assert.Contains(t, message, "Usage:", "Help message should contain usage information")

	// Test case 3: cherry-pick-invite command
	// This would require mocking the GitHub token in the config and the GitHub API
	// For simplicity, we're just testing that the command is recognized
	command = &Command{
		Name: "/cherry-pick-invite",
		Args: []string{},
		Sender: &CommandSender{
			Email: "test@example.com",
		},
	}
	_, err = handler.handleCommand(ctx, command)
	assert.Error(t, err, "Should return error when arguments are missing")

	// Test case 4: create_hotfix_branch command
	command = &Command{
		Name: "/create_hotfix_branch",
		Args: []string{},
		Sender: &CommandSender{
			Email: "test@example.com",
		},
	}
	_, err = handler.handleCommand(ctx, command)
	assert.Error(t, err, "Should return error when arguments are missing")
}
