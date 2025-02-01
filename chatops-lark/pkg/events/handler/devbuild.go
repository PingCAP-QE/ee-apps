package handler

import (
	"context"
	"fmt"
)

// TODO: get it from cli args.
const devBuildURL = "https://tibuild.pingcap.net/api/devbuilds"

func runCommandDevbuild(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		return "", fmt.Errorf("missing subcommand")
	}

	subCmd := args[0]
	switch subCmd {
	case "trigger":
		return runCommandDevbuildTrigger(ctx, args[1:])
	case "poll":
		return runCommandDevbuildPoll(ctx, args[1:])
	case "-h", "--help":
		return "Usage: devbuild <subcommand> [args...]\n\nSubcommands:\n  trigger\n  poll", nil
	default:
		return "", fmt.Errorf("unknown subcommand: %s", subCmd)
	}
}
