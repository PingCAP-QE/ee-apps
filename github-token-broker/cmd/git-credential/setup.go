package main

import (
	"errors"
	"flag"
	"fmt"
	"io"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/gitcred"
)

const setupCommand = "setup"

// runSetup renders the credential helper configuration into the pod's shared
// home directory. It is the first thing a Tekton setup-git-auth step runs.
func runSetup(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet(setupCommand, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	home := flags.String("home", "/tekton/home", "shared home directory of the pod")
	brokerURL := flags.String("broker-url", "", "token broker base URL")
	permissions := flags.String("permissions", "contents=read", "name=level pairs separated by semicolons")
	hosts := flags.String("hosts", "github.com", "comma separated git hosts")
	fallback := flags.String("fallback-credentials", "", "optional .git-credentials fallback file")
	callerTokenFile := flags.String("token-file", "", "optional ServiceAccount token path override")
	if err := flags.Parse(args); err != nil {
		return fmt.Errorf("parse setup flags: %w", err)
	}
	if flags.NArg() > 0 {
		return fmt.Errorf("unexpected setup arguments %v", flags.Args())
	}
	if *brokerURL == "" {
		return errors.New("setup requires -broker-url")
	}

	result, err := gitcred.Setup(gitcred.SetupOptions{
		Home:                *home,
		BrokerURL:           *brokerURL,
		Permissions:         *permissions,
		Hosts:               *hosts,
		FallbackCredentials: *fallback,
		CallerTokenFile:     *callerTokenFile,
	})
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "git-auth: helper %s\n", result.CredentialHelper)
	fmt.Fprintf(stdout, "git-auth: config %s\n", result.ConfigFile)
	fmt.Fprintf(stdout, "git-auth: gitconfig fragment %s\n", result.GitConfigFragment)
	fmt.Fprintf(stdout, "git-auth: credential source file %s (app or pat)\n", result.StateFile)
	fmt.Fprintf(stdout, "git-auth: hosts %v, %d permission(s)\n", result.RequestedHosts, result.RequestedPermCount)
	return nil
}
