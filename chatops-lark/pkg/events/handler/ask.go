package handler

import (
	"context"
	"fmt"
	"strings"

	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/rs/zerolog/log"
	"github.com/sashabaranov/go-openai"
	"gopkg.in/yaml.v3"
)

const (
	askHelpText = `missing question

Usage: /ask <question...>

Example:
  /ask What is the on-call schedule for the infra team this week?
  /ask How do I debug error code 1234 in service X?

For more details, use: /ask --help
`

	askDetailedHelpText = `Usage: /ask <question...>

Description:
  Asks an AI assistant a question. The assistant may leverage internal tools (MCP context)
  to provide relevant and up-to-date information alongside its general knowledge.

Examples:
  /ask What is the current status of the main production cluster?
  /ask Explain the purpose of the 'widget-processor' microservice.
  /ask Summarize the recent alerts for the database tier.
  /ask How do I request access to the staging environment?

Use '/ask --help' or '/ask -h' to see this message.
`
)

// runCommandAsk handles the /ask command logic.
func runCommandAsk(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		// No question provided
		return "", fmt.Errorf(askHelpText)
	}

	// Check for help flags explicitly, as there are no subcommands
	firstArg := args[0]
	if firstArg == "-h" || firstArg == "--help" {
		if len(args) == 1 {
			return askDetailedHelpText, nil
		}
		// Allow asking help *about* something, e.g. /ask --help tools
		// But for now, just treat any args after --help as part of the help request itself.
		// Let's just return the detailed help for simplicity.
		// Alternatively, could error out:
		// return "", fmt.Errorf("unknown arguments after %s: %v", firstArg, args[1:])
		return askDetailedHelpText, nil
	}

	// The entire argument list forms the question
	question := strings.Join(args, " ")

	// --- Placeholder for actual AI/Tool interaction ---
	// Here you would:
	// 1. Parse the question for intent or specific tool requests (if applicable).
	// 2. Potentially query MCP tools based on the question to gather context.
	// 3. Format a prompt including the user's question and any gathered context.
	// 4. Send the prompt to the configured LLM.
	// 5. Receive the LLM's response.
	// 6. Format the response for Lark.
	result, err := processAskRequest(ctx, question)
	if err != nil {
		return "", fmt.Errorf("failed to process ask request: %w", err)
	}
	// --- End Placeholder ---

	return result, nil
}

// processAskRequest is a placeholder for the core logic interacting with the LLM and tools.
// TODO: Implement the actual interaction with the AI model and MCP tools.
func processAskRequest(ctx context.Context, question string) (string, error) {
	// Simulate processing and generating a response
	// In a real implementation, this would involve API calls to an LLM service
	// and potentially calls to internal "MCP tools" APIs.
	fmt.Printf("Processing ask request for question: %q\n", question) // Log for debugging

	openaiCfg := ctx.Value(ctxKeyOpenAIConfig)
	openaiModel := ctx.Value(ctxKeyOpenAIModel)
	systemPrompt := ctx.Value(ctxKeyOpenAISystemPrompt)
	log.Debug().
		Any("base_url", openaiCfg.(openai.ClientConfig).BaseURL).
		Any("systemPrompt", systemPrompt).
		Msg("debug vars")
	client := openai.NewClientWithConfig(openaiCfg.(openai.ClientConfig))

	resp, err := client.CreateChatCompletion(
		context.Background(),
		openai.ChatCompletionRequest{
			Model: openaiModel.(string),
			Messages: []openai.ChatCompletionMessage{
				{
					Role:    openai.ChatMessageRoleSystem,
					Content: systemPrompt.(string),
				},
				{
					Role:    openai.ChatMessageRoleUser,
					Content: question,
				},
			},
		},
	)

	if err != nil {
		log.Err(err).Msg("failed to create chat completion")
		return "", fmt.Errorf("failed to create chat completion: %w", err)
	}
	response := resp.Choices[0].Message.Content

	// // Example of how context *could* be added (replace with actual tool calls)
	// if strings.Contains(strings.ToLower(question), "status") && strings.Contains(strings.ToLower(question), "production") {
	// 	// Pretend we called an MCP tool for cluster status
	// 	mcpContext := "\n[MCP Tool Context: Production cluster status is currently nominal.]"
	// 	response += mcpContext
	// }

	// // Simulate potential error
	// if strings.Contains(strings.ToLower(question), "error simulation") {
	// 	return "", fmt.Errorf("simulated error during AI processing")
	// }

	return response, nil
}

func listMcpTools(ctx context.Context, mcpClient *client.SSEMCPClient, _ string) (string, error) {
	ret, err := mcpClient.ListTools(ctx, mcp.ListToolsRequest{})
	if err != nil {
		return "", err
	}
	bytes, _ := yaml.Marshal(ret)

	return string(bytes), nil
}
