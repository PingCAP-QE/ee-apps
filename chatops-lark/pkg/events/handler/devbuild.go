package handler

import (
	"context"
	"fmt"
)

// TODO: get it from cli args.
const devBuildURL = "https://tibuild.pingcap.net/api/devbuilds"

const (
	devBuildHelpText = `missing subcommand

Usage: /devbuild <subcommand> [args...]

Subcommands:
  trigger <product> <version> <gitRef> [options]  - Trigger a new dev build
  poll <buildID>                                  - Poll the status of a build

Examples:
  /devbuild trigger tidb v6.5.0 master -e enterprise
  /devbuild trigger tikv v6.5.0 master --features failpoint
  /devbuild trigger tiflash v6.5.0 master --pushGCR
  /devbuild poll 12345

For more details, use: /devbuild --help
`

	devBuildDetailedHelpText = `Usage: /devbuild <subcommand> [args...]

Subcommands:
  trigger <product> <version> <gitRef> [options]  - Trigger a new dev build
  poll <buildID>                                  - Poll the status of a build

Examples:
  /devbuild trigger tidb v6.5.0 master -e enterprise
  /devbuild trigger tikv v6.5.0 master --features failpoint
  /devbuild trigger tiflash v6.5.0 master --pushGCR
  /devbuild poll 12345

Options for trigger:
  -e, --edition string      Product edition (community or enterprise, default: community)
  -p, --platform string		Build for platform (linux/amd64, linux/arm64, darwin/amd64 or darwin/arm64, default: all), only support when the engine is tekton.
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
  --engine string           Pipeline engine (jenkins or tekton, default: jenkins)
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
		return devBuildDetailedHelpText, nil
	default:
		return "", fmt.Errorf("unknown subcommand: %s", subCmd)
	}
}
