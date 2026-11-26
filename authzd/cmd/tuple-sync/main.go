// Command tuple-sync reconciles one reviewed relationship snapshot into
// OpenFGA. It is a one-shot job: run it from a reviewed change pipeline or a
// scheduled Job, never from request-serving code.
//
// The job owns only the object types and relations named by --object-type and
// --relation. Everything else in the store is left untouched. It refuses to run
// on an incomplete snapshot, and it refuses to delete every owned tuple unless
// --allow-empty is set after an operator confirmed the source is authoritative
// empty rather than failed.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/openfga"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/relationships"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/workspace"
)

func main() {
	var stdout, stderr strings.Builder
	code := run(os.Args[1:], os.Getenv, &stdout, &stderr)
	_, _ = io.WriteString(os.Stdout, stdout.String())
	_, _ = io.WriteString(os.Stderr, stderr.String())
	os.Exit(code)
}

func run(args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("tuple-sync", flag.ContinueOnError)
	flags.SetOutput(stderr)
	snapshotPath := flags.String("snapshot", "", "path to the reviewed relationship snapshot (--source file)")
	sourceKind := flags.String("source", "file", "relationship source: file or workspace-group")
	workspaceGroup := flags.String("workspace-group", "", "Workspace group key for --source workspace-group")
	teamObject := flags.String("team-object", "", "team object that receives members, e.g. team:all-staff")
	workspaceTokenFile := flags.String("workspace-token-file", "", "file holding a Directory API bearer token")
	directoryURL := flags.String("directory-url", "", "override the Directory API base URL (tests)")
	verifyEmail := flags.String("verify-email", "", "with --verify-subject, the Directory user to compare")
	verifySubject := flags.String("verify-subject", "", "with --verify-email, the expected token subject (Gate 1)")
	objectTypes := flags.String("object-type", "", "comma-separated object types this job owns")
	relations := flags.String("relation", "", "comma-separated relations this job owns")
	dryRun := flags.Bool("dry-run", false, "compute and report the plan without writing tuples")
	allowEmpty := flags.Bool("allow-empty", false, "allow a reviewed snapshot to delete every owned tuple")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	scope := relationships.Scope{ObjectTypes: splitList(*objectTypes), Relations: splitList(*relations)}
	if len(scope.ObjectTypes) == 0 || len(scope.Relations) == 0 {
		fmt.Fprintln(stderr, "tuple-sync: --object-type and --relation are required")
		return 2
	}

	logger := slog.New(slog.NewJSONHandler(stderr, nil))
	metrics := relationships.NewMetrics()
	source, err := buildSource(sourceConfig{
		kind: *sourceKind, snapshotPath: *snapshotPath, group: *workspaceGroup,
		teamObject: *teamObject, tokenFile: *workspaceTokenFile, directoryURL: *directoryURL,
		verifyEmail: *verifyEmail, verifySubject: *verifySubject,
	}, logger)
	if err != nil {
		logger.Error("invalid relationship source", "error", err)
		fmt.Fprintln(stderr, "tuple-sync:", err)
		return 2
	}
	store, err := openfga.NewTupleStore(openfga.Config{
		APIURL:   strings.TrimSpace(getenv("AUTHZD_OPENFGA_URL")),
		StoreID:  strings.TrimSpace(getenv("AUTHZD_OPENFGA_STORE_ID")),
		ModelID:  strings.TrimSpace(getenv("AUTHZD_OPENFGA_MODEL_ID")),
		APIToken: strings.TrimSpace(getenv("AUTHZD_OPENFGA_API_TOKEN")),
	})
	if err != nil {
		logger.Error("invalid OpenFGA configuration", "error", err)
		return 2
	}
	reconciler, err := relationships.NewReconciler(source, store, scope)
	if err != nil {
		logger.Error("invalid reconcile configuration", "error", err)
		return 2
	}
	reconciler.WithReporter(metrics).WithDryRun(*dryRun).WithEmptySnapshotAllowed(*allowEmpty)

	result, reconcileErr := reconciler.Reconcile(context.Background())
	_, _ = io.WriteString(stdout, metrics.Render())
	logger.Info("relationship sync finished",
		"outcome", relationships.Outcome(result, reconcileErr),
		"source", result.Source, "revision", result.Revision,
		"desired", result.Desired, "writes", result.Writes, "deletes", result.Deletes,
		"lag_seconds", int64(result.Lag.Seconds()), "dry_run", *dryRun,
		"error", errorText(reconcileErr),
	)
	if reconcileErr != nil {
		if errors.Is(reconcileErr, relationships.ErrIncompleteSnapshot) ||
			errors.Is(reconcileErr, relationships.ErrEmptySnapshot) {
			fmt.Fprintln(stderr, "tuple-sync: refusing to reconcile:", reconcileErr)
		}
		return 1
	}
	return 0
}

type sourceConfig struct {
	kind, snapshotPath, group, teamObject, tokenFile, verifyEmail, verifySubject, directoryURL string
}

// buildSource selects the relationship source. The workspace source verifies
// Gate 1 first when asked: writing tuples from a Directory id that is not the
// token subject would authorize the wrong person, so a mismatch aborts before the
// store is touched.
func buildSource(cfg sourceConfig, logger *slog.Logger) (relationships.Source, error) {
	switch strings.TrimSpace(cfg.kind) {
	case "file", "":
		if strings.TrimSpace(cfg.snapshotPath) == "" {
			return nil, errors.New("--snapshot is required for --source file")
		}
		return relationships.NewFileSource(cfg.snapshotPath)
	case "workspace-group":
		if strings.TrimSpace(cfg.group) == "" || strings.TrimSpace(cfg.teamObject) == "" {
			return nil, errors.New("--workspace-group and --team-object are required for --source workspace-group")
		}
		if strings.TrimSpace(cfg.tokenFile) == "" {
			return nil, errors.New("--workspace-token-file is required for --source workspace-group")
		}
		token := tokenFromFile(cfg.tokenFile)
		if strings.TrimSpace(cfg.verifySubject) != "" || strings.TrimSpace(cfg.verifyEmail) != "" {
			if strings.TrimSpace(cfg.verifySubject) == "" || strings.TrimSpace(cfg.verifyEmail) == "" {
				return nil, errors.New("--verify-subject and --verify-email must be set together")
			}
			verifier, err := workspace.NewVerifier(workspace.VerifierConfig{Token: token, BaseURL: cfg.directoryURL})
			if err != nil {
				return nil, err
			}
			if err := verifier.VerifySubject(context.Background(), cfg.verifyEmail, cfg.verifySubject); err != nil {
				return nil, err
			}
			logger.Info("gate 1 verified", "email", cfg.verifyEmail)
		}
		return workspace.NewSource(workspace.Config{
			BaseURL: cfg.directoryURL, GroupKey: cfg.group, TeamObject: cfg.teamObject, Token: token,
		})
	default:
		return nil, fmt.Errorf("unknown --source %q", cfg.kind)
	}
}

// tokenFromFile re-reads the file on every call so a rotated token is picked up
// without restarting the job.
func tokenFromFile(path string) func(context.Context) (string, error) {
	return func(context.Context) (string, error) {
		data, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("read workspace token: %w", err)
		}
		return strings.TrimSpace(string(data)), nil
	}
}

func splitList(value string) []string {
	var items []string
	for _, item := range strings.Split(value, ",") {
		if trimmed := strings.TrimSpace(item); trimmed != "" {
			items = append(items, trimmed)
		}
	}
	return items
}

func errorText(err error) string {
	if err == nil {
		return ""
	}
	message := err.Error()
	if len(message) > 512 {
		return message[:512]
	}
	return message
}
