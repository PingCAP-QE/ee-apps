// Command git-credential-broker is a git credential helper that mints GitHub
// App installation tokens from the in-cluster token broker.
//
// Git calls the helper as:
//
//	git-credential-broker [-config <file>] get|store|erase
//
// With -config the helper reads a JSON configuration file written by the Tekton
// setup-git-auth step; without it the configuration comes from GIT_AUTH_*
// environment variables.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/gitcred"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "git-credential-broker: %v\n", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) error {
	cfg, action, err := parseArgs(args)
	if err != nil {
		return err
	}
	if action != "get" && action != "store" && action != "erase" {
		return fmt.Errorf("unsupported operation %q, want get, store or erase", action)
	}
	request, err := gitcred.ParseRequest(stdin)
	if err != nil {
		return fmt.Errorf("parse credential request: %w", err)
	}

	cfg.Logger = gitcred.NewLogger(stderr)
	helper, err := gitcred.New(cfg)
	if err != nil {
		return err
	}

	switch action {
	case "get":
		response, err := helper.Get(ctx, request)
		if err != nil {
			return err
		}
		return response.Write(stdout)
	case "store":
		// Tokens are cached by the helper itself; git's store operation has
		// nothing to persist.
		return nil
	default: // erase
		return helper.Erase(request)
	}
}

// parseArgs splits git's arguments (the configured flags plus the trailing
// operation) into configuration and operation.
func parseArgs(args []string) (gitcred.Config, string, error) {
	if len(args) == 0 {
		return gitcred.Config{}, "", errors.New("expected an operation: get, store or erase")
	}
	flags := flag.NewFlagSet("git-credential-broker", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	configPath := flags.String("config", "", "path to the JSON configuration file")
	// Git appends the operation after any configured arguments, so parse the
	// leading flags and treat the last argument as the operation.
	action := args[len(args)-1]
	if err := flags.Parse(args[:len(args)-1]); err != nil {
		return gitcred.Config{}, "", fmt.Errorf("parse helper flags: %w", err)
	}
	if flags.NArg() > 0 {
		return gitcred.Config{}, "", fmt.Errorf("unexpected helper arguments %v", flags.Args())
	}

	if *configPath != "" {
		cfg, err := gitcred.ConfigFromFile(*configPath)
		if err != nil {
			return gitcred.Config{}, "", err
		}
		return cfg, action, nil
	}
	cfg, err := gitcred.ConfigFromEnv(os.Getenv)
	if err != nil {
		return gitcred.Config{}, "", err
	}
	return cfg, action, nil
}
