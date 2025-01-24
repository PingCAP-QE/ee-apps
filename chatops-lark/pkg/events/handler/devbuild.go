package handler

import (
	"fmt"
)

// TODO: get it from cli args.
const devBuildURL = "https://tibuild.pingcap.net/api/devbuilds"

func runCommandDevbuild(args []string, sender *CommandSender) (string, error) {
	if len(args) == 0 {
		return "", fmt.Errorf("missing subcommand")
	}

	subCmd := args[0]
	switch subCmd {
	case "trigger":
		return runCommandDevbuildTrigger(args[1:], sender)
	case "poll":
		return runCommandDevbuildPoll(args[1:])
	default:
		return "", fmt.Errorf("unknown subcommand: %s", subCmd)
	}
}
