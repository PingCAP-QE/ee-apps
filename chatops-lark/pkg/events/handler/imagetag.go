package handler

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

const (
	cfgKeyImageTagOwner          = "image_tag.owner"
	cfgKeyImageTagRepo           = "image_tag.repo"
	cfgKeyImageTagWorkflow       = "image_tag.workflow"
	cfgKeyImageTagRef            = "image_tag.ref"
	cfgKeyImageTagCredentialRefs = "image_tag.credential_refs"

	defaultImageTagOwner    = "tidbcloud"
	defaultImageTagRepo     = "docker-image-controller"
	defaultImageTagWorkflow = "query-image-tag.yml"
)

const imageTagHelpText = `Usage: /image-tag <subcommand> [args...]

Subcommands:
  trigger <registry> <tag>  - Trigger a GitHub Actions image-tag query
  poll <runID>              - Poll a workflow run

Examples:
  /image-tag trigger ghcr.io/pingcap/tidb nightly
  /image-tag trigger registry.example.com/team/image v8.5.0
  /image-tag poll 123456789

Notes:
  - <registry> must be the image reference without the tag.
  - GitHub authentication comes from backend config, never from chat input.

Use '/image-tag --help' or '/image-tag -h' to see this message.
`

type imageTagWorkflowConfig struct {
	Owner          string
	Repo           string
	Workflow       string
	Ref            string
	CredentialRefs map[string]string
}

func runCommandImageTag(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		return "", errors.New(imageTagHelpText)
	}

	switch args[0] {
	case "trigger":
		return runCommandImageTagTrigger(ctx, args[1:])
	case "poll":
		return runCommandImageTagPoll(ctx, args[1:])
	case "-h", "--help":
		return imageTagHelpText, NewInformationError("Requested command usage")
	default:
		return "", fmt.Errorf("unknown subcommand: %s", args[0])
	}
}

func setupCtxImageTag(ctx context.Context, cfg config.Config, _ *CommandActor) context.Context {
	owner := strings.TrimSpace(cfg.ImageTag.Owner)
	if owner == "" {
		owner = defaultImageTagOwner
	}

	repo := strings.TrimSpace(cfg.ImageTag.Repo)
	if repo == "" {
		repo = defaultImageTagRepo
	}

	workflow := strings.TrimSpace(cfg.ImageTag.Workflow)
	if workflow == "" {
		workflow = defaultImageTagWorkflow
	}

	nextCtx := context.WithValue(ctx, ctxKeyGithubToken, strings.TrimSpace(cfg.ImageTag.GitHubToken))
	nextCtx = context.WithValue(nextCtx, cfgKeyImageTagOwner, owner)
	nextCtx = context.WithValue(nextCtx, cfgKeyImageTagRepo, repo)
	nextCtx = context.WithValue(nextCtx, cfgKeyImageTagWorkflow, workflow)
	nextCtx = context.WithValue(nextCtx, cfgKeyImageTagRef, strings.TrimSpace(cfg.ImageTag.Ref))
	nextCtx = context.WithValue(nextCtx, cfgKeyImageTagCredentialRefs, normalizeImageTagCredentialRefs(cfg.ImageTag.CredentialRefs))

	return nextCtx
}

func loadImageTagWorkflowConfig(ctx context.Context) (imageTagWorkflowConfig, string, error) {
	token, ok := ctx.Value(ctxKeyGithubToken).(string)
	if !ok || token == "" {
		return imageTagWorkflowConfig{}, "", fmt.Errorf("GitHub token not found in context")
	}

	owner, ok := ctx.Value(cfgKeyImageTagOwner).(string)
	if !ok || owner == "" {
		return imageTagWorkflowConfig{}, "", fmt.Errorf("image tag owner not found in context")
	}

	repo, ok := ctx.Value(cfgKeyImageTagRepo).(string)
	if !ok || repo == "" {
		return imageTagWorkflowConfig{}, "", fmt.Errorf("image tag repo not found in context")
	}

	workflow, ok := ctx.Value(cfgKeyImageTagWorkflow).(string)
	if !ok || workflow == "" {
		return imageTagWorkflowConfig{}, "", fmt.Errorf("image tag workflow not found in context")
	}

	ref, _ := ctx.Value(cfgKeyImageTagRef).(string)
	credentialRefs, _ := ctx.Value(cfgKeyImageTagCredentialRefs).(map[string]string)

	return imageTagWorkflowConfig{
		Owner:          owner,
		Repo:           repo,
		Workflow:       workflow,
		Ref:            ref,
		CredentialRefs: credentialRefs,
	}, token, nil
}

func normalizeImageTagCredentialRefs(credentialRefs map[string]string) map[string]string {
	if len(credentialRefs) == 0 {
		return nil
	}

	normalized := make(map[string]string, len(credentialRefs))
	for prefix, credentialRef := range credentialRefs {
		prefix = strings.TrimSpace(prefix)
		credentialRef = strings.TrimSpace(credentialRef)
		if prefix == "" || credentialRef == "" {
			continue
		}
		normalized[prefix] = credentialRef
	}
	if len(normalized) == 0 {
		return nil
	}

	return normalized
}
