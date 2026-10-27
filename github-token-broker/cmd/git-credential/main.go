// Command git-credential-broker is a git credential helper that mints GitHub
// App installation tokens from the in-cluster token broker.
//
// Usage (configured by git through credential.helper):
//
//	git-credential-broker get
//	git-credential-broker store
//	git-credential-broker erase
//
// Configuration comes from GIT_AUTH_* environment variables.
package main

import (
	"context"
	"errors"
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
	if len(args) != 1 {
		return errors.New("expected exactly one operation: get, store or erase")
	}
	action := args[0]
	request, err := gitcred.ParseRequest(stdin)
	if err != nil {
		return fmt.Errorf("parse credential request: %w", err)
	}

	cfg, err := gitcred.ConfigFromEnv(os.Getenv)
	if err != nil {
		return err
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
	case "erase":
		return helper.Erase(request)
	default:
		return fmt.Errorf("unsupported operation %q, want get, store or erase", action)
	}
}
