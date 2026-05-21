package handler

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

const (
	cfgKeyRegistryImageOwner          = "registry_image.owner"
	cfgKeyRegistryImageRepo           = "registry_image.repo"
	cfgKeyRegistryImageWorkflow       = "registry_image.workflow"
	cfgKeyRegistryImageRef            = "registry_image.ref"
	cfgKeyRegistryImageCredentialRefs = "registry_image.credential_refs"

	defaultRegistryImageOwner    = "tidbcloud"
	defaultRegistryImageRepo     = "docker-image-controller"
	defaultRegistryImageWorkflow = "query-image-tag.yml"
)

const registryImageHelpText = `Usage: /registry-image <subcommand> [args...]

Subcommands:
  query <image:tag>          - Query a cloud registry image by full tag reference
  query <image> --tag <tag>  - Query a cloud registry image with an explicit tag
  inspect ...                - Alias for query

Examples:
  /registry-image query ghcr.io/pingcap/tidb:nightly
  /registry-image query registry.example.com/team/image --tag v8.5.0
  /registry-image inspect tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm:v26.3.0-nextgen

Notes:
  - This command checks whether the target tag exists in the cloud registry and returns OCI image metadata when found.
  - Image Created At (OCI) is OCI image metadata, not the registry push/sync time.
  - GitHub authentication comes from backend config, never from chat input.

Use '/registry-image --help' or '/registry-image -h' to see this message.
`

type registryImageWorkflowConfig struct {
	Owner          string
	Repo           string
	Workflow       string
	Ref            string
	CredentialRefs map[string]string
}

func runCommandRegistryImage(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		return "", errors.New(registryImageHelpText)
	}

	switch args[0] {
	case "query", "inspect":
		return runCommandRegistryImageQuery(ctx, args[1:])
	case "-h", "--help":
		return registryImageHelpText, NewInformationError("Requested command usage")
	default:
		return "", fmt.Errorf("unknown subcommand: %s", args[0])
	}
}

func setupCtxRegistryImage(ctx context.Context, cfg config.Config, _ *CommandActor) context.Context {
	registryImageCfg := cfg.EffectiveRegistryImage()
	if registryImageCfg == nil {
		return ctx
	}

	owner := strings.TrimSpace(registryImageCfg.Owner)
	if owner == "" {
		owner = defaultRegistryImageOwner
	}

	repo := strings.TrimSpace(registryImageCfg.Repo)
	if repo == "" {
		repo = defaultRegistryImageRepo
	}

	workflow := strings.TrimSpace(registryImageCfg.Workflow)
	if workflow == "" {
		workflow = defaultRegistryImageWorkflow
	}

	nextCtx := context.WithValue(ctx, ctxKeyGithubToken, strings.TrimSpace(registryImageCfg.GitHubToken))
	nextCtx = context.WithValue(nextCtx, cfgKeyRegistryImageOwner, owner)
	nextCtx = context.WithValue(nextCtx, cfgKeyRegistryImageRepo, repo)
	nextCtx = context.WithValue(nextCtx, cfgKeyRegistryImageWorkflow, workflow)
	nextCtx = context.WithValue(nextCtx, cfgKeyRegistryImageRef, strings.TrimSpace(registryImageCfg.Ref))
	nextCtx = context.WithValue(nextCtx, cfgKeyRegistryImageCredentialRefs, normalizeRegistryImageCredentialRefs(registryImageCfg.CredentialRefs))

	return nextCtx
}

func loadRegistryImageWorkflowConfig(ctx context.Context) (registryImageWorkflowConfig, string, error) {
	token, ok := ctx.Value(ctxKeyGithubToken).(string)
	if !ok || token == "" {
		return registryImageWorkflowConfig{}, "", fmt.Errorf("GitHub token not found in context")
	}

	owner, ok := ctx.Value(cfgKeyRegistryImageOwner).(string)
	if !ok || owner == "" {
		return registryImageWorkflowConfig{}, "", fmt.Errorf("registry image owner not found in context")
	}

	repo, ok := ctx.Value(cfgKeyRegistryImageRepo).(string)
	if !ok || repo == "" {
		return registryImageWorkflowConfig{}, "", fmt.Errorf("registry image repo not found in context")
	}

	workflow, ok := ctx.Value(cfgKeyRegistryImageWorkflow).(string)
	if !ok || workflow == "" {
		return registryImageWorkflowConfig{}, "", fmt.Errorf("registry image workflow not found in context")
	}

	ref, _ := ctx.Value(cfgKeyRegistryImageRef).(string)
	credentialRefs, _ := ctx.Value(cfgKeyRegistryImageCredentialRefs).(map[string]string)

	return registryImageWorkflowConfig{
		Owner:          owner,
		Repo:           repo,
		Workflow:       workflow,
		Ref:            ref,
		CredentialRefs: credentialRefs,
	}, token, nil
}

func normalizeRegistryImageCredentialRefs(credentialRefs map[string]string) map[string]string {
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
