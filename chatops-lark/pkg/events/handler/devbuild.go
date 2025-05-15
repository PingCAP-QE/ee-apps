package handler

import (
	"context"
	"fmt"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

// Configuration key for DevBuild URL in the config map
const cfgKeyDevBuildURL = "devbuild.api_url"

const (
	devBuildHelpText = `missing subcommand

Usage: /devbuild <subcommand> [args...]

Subcommands:
  trigger [options]  - Trigger a new dev build
  poll <buildID>     - Poll the status of a build

Examples:
  /devbuild trigger --product tidb --version v6.5.0 --gitRef branch/master -e enterprise
  /devbuild trigger --product tikv --version v6.5.0 --gitRef branch/master --features failpoint
  /devbuild trigger --product tiflash --version v6.5.0 --gitRef branch/master --pushGCR
  /devbuild poll 12345

For more details, use: /devbuild --help
`

	devBuildDetailedHelpText = `Usage: /devbuild <subcommand> [args...]

Subcommands:
  trigger [options]  - Trigger a new dev build
  poll <buildID>     - Poll the status of a build

Examples:
  /devbuild trigger --product tidb --version v6.5.0 --gitRef branch/master -e enterprise
  /devbuild trigger --product tikv --version v6.5.0 --gitRef branch/master --features failpoint
  /devbuild trigger --product tiflash --version v6.5.0 --gitRef branch/master --pushGCR
  /devbuild poll 12345

Required options for trigger:
  --product string          Product to build (tidb, tikv, pd, etc.)
  --version string          Version to build (v1.2.3)
  --gitRef string           Git reference to build from (branch/<branch-name>, tag/<tag-name>, commit/<commit-sha> or pull/<number>)

Optional options for trigger:
  -e, --edition string      Product edition (community or enterprise, default: community)
  -p, --platform string     Build for platform (linux/amd64, linux/arm64, darwin/amd64 or darwin/arm64, default: all), only support when the engine is tekton.
  --pluginGitRef string     Git reference for plugins (only for enterprise tidb)
  --pushGCR                 Whether to push to GCR (default: false)
  --githubRepo string       GitHub repository (for forked repos)
  --features string         Build features (e.g., failpoint)
  --dryrun                  Dry run without actual build (default: false)
  --buildEnv string         Build environment variables (can be specified multiple times)
  --productDockerfile string Dockerfile URL for product
  --productBaseImg string   Product base image
  --builderImg string       Docker image for builder
  --targetImg string        Target image
  --engine string           Pipeline engine (jenkins or tekton, default: jenkins, 'tekton' is in beta)
`
)

func runCommandDevbuild(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		return "", fmt.Errorf(devBuildHelpText)
	}

	subCmd := args[0]
	switch subCmd {
	case "trigger":
		return runCommandDevbuildTrigger(ctx, args[1:])
	case "poll":
		return runCommandDevbuildPoll(ctx, args[1:])
	case "-h", "--help":
		return devBuildDetailedHelpText, NewInformationError("Requested command usage")
	default:
		return "", fmt.Errorf("unknown subcommand: %s", subCmd)
	}
}

func setupCtxDevbuild(ctx context.Context, config config.Config, sender *CommandActor) context.Context {
	newCtx := context.WithValue(ctx, ctxKeyLarkSenderEmail, sender.Email)
	newCtx = context.WithValue(newCtx, cfgKeyDevBuildURL, config.DevBuild.ApiURL)

	return newCtx
}
