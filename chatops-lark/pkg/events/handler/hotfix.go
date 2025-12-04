package handler

import (
	"context"
	"flag"
	"fmt"
	"strings"
	"time"

	"github.com/go-resty/resty/v2"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

// ctx keys store hotfix service configuration
const hotfixCfgKey string = "hotfix.cfg"

type hotfixRuntimeConfig struct {
	APIURL      string
	ActorEmail  string
	ActorGitHub *string
}

// setupCtxHotfix prepares the runtime context for hotfix-related commands.
func setupCtxHotfix(ctx context.Context, cfg config.Config, actor *CommandActor) context.Context {
	runtime := hotfixRuntimeConfig{
		APIURL:      cfg.Hotfix.ApiURL,
		ActorEmail:  actor.Email,
		ActorGitHub: actor.GitHubID,
	}
	return context.WithValue(ctx, hotfixCfgKey, &runtime)
}

type bumpTidbxParams struct {
	repo   string
	commit string
	help   bool
}

func parseCommandHotfixBumpTidbx(args []string) (*bumpTidbxParams, string, error) {
	fs := flag.NewFlagSet("/bump-tidbx-hotfix-tag", flag.ContinueOnError)
	// silence default usage output
	sink := new(strings.Builder)
	fs.SetOutput(sink)

	ret := &bumpTidbxParams{}

	fs.StringVar(&ret.repo, "repo", "", "Full name of GitHub repository (e.g., pingcap/tidb)")
	fs.StringVar(&ret.commit, "commit", "", "Short or full git commit SHA")
	fs.BoolVar(&ret.help, "help", false, "Show help")

	if err := fs.Parse(args); err != nil {
		return nil, "", err
	}

	if ret.help {
		return ret, hotfixHelpText(), NewSkipError("Help requested")
	}

	// validate required args
	missing := []string{}
	if ret.repo == "" {
		missing = append(missing, "--repo")
	}
	if ret.commit == "" {
		missing = append(missing, "--commit")
	}
	if len(missing) > 0 {
		return nil, hotfixHelpText(), NewInformationError(fmt.Sprintf("Missing required argument(s): %s", strings.Join(missing, ", ")))
	}

	// basic repo format validation
	if !strings.Contains(ret.repo, "/") {
		return nil, hotfixHelpText(), NewInformationError("Invalid --repo. Expected format: <org>/<repo> (e.g., pingcap/tidb)")
	}

	// commit sanity
	if len(ret.commit) < 7 || len(ret.commit) > 40 {
		// sha length can vary, but common short SHA >=7, full 40
		// continue but inform the user
		return ret, "", NewInformationError("The provided --commit looks unusual; ensure it's a valid short or full SHA")
	}

	return ret, "", nil
}

func hotfixHelpText() string {
	return `Usage: /bump-tidbx-hotfix-tag --repo <org>/<repo> --commit <commit-sha>

Description:
  Bump TiDB-X style hotfix git tag for a GitHub repository by calling TiBuild v2 API.

Arguments:
  --repo     Full name of GitHub repository (e.g., pingcap/tidb)
  --commit   Short or full git commit SHA to tag

Examples:
  /bump-tidbx-hotfix-tag --repo pingcap/tidb --commit abc123def

Notes:
  The tag will be generated in TiDB-X style and created on the specified commit.`
}

// runCommandHotfixBumpTidbxTag handles `/bump-tidbx-hotfix-tag` command.
func runCommandHotfixBumpTidbxTag(ctx context.Context, args []string) (string, error) {
	params, msg, err := parseCommandHotfixBumpTidbx(args)
	if err != nil {
		// return parsed message and error type to upper layer
		return msg, err
	}

	runtime, ok := ctx.Value(hotfixCfgKey).(*hotfixRuntimeConfig)
	if !ok || runtime == nil || runtime.APIURL == "" {
		return "", fmt.Errorf("hotfix API URL is not configured")
	}

	// construct request payload according to tibuild v2 goa design
	type request struct {
		Repo   string `json:"repo"`
		Author string `json:"author"`
		Commit string `json:"commit,omitempty"`
		// Branch is optional in design; we don't require it in command, leave empty
		Branch string `json:"branch,omitempty"`
	}
	reqBody := request{
		Repo:   params.repo,
		Commit: params.commit,
		Author: preferAuthor(runtime),
	}

	// POST to /bump-tag-for-tidbx
	url := strings.TrimRight(runtime.APIURL, "/") + "/bump-tag-for-tidbx"

	client := resty.New().SetTimeout(20 * time.Second)
	var res struct {
		Repo   string `json:"repo"`
		Commit string `json:"commit"`
		Tag    string `json:"tag"`
	}
	var apiErr struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	}

	r, err := client.R().
		SetBody(reqBody).
		SetResult(&res).
		SetError(&apiErr).
		Post(url)
	if err != nil {
		log.Err(err).Msg("hotfix API request failed")
		return "", fmt.Errorf("hotfix API request failed: %w", err)
	}
	if !r.IsSuccess() {
		if apiErr.Message != "" {
			return "", fmt.Errorf("hotfix API error: %s (code: %d, http: %d)", apiErr.Message, apiErr.Code, r.StatusCode())
		}
		return "", fmt.Errorf("hotfix API http status: %d", r.StatusCode())
	}

	// build user-friendly message
	lines := []string{
		"TiDB-X hotfix tag bumped successfully:",
		fmt.Sprintf("• Repo:   %s", res.Repo),
		fmt.Sprintf("• Commit: %s", res.Commit),
		fmt.Sprintf("• Tag:    %s", res.Tag),
	}
	return strings.Join(lines, "\n"), nil
}

// preferAuthor picks a string representing the author for the API.
// If GitHubID is available, prefer it; otherwise fall back to email.
func preferAuthor(rt *hotfixRuntimeConfig) string {
	if rt.ActorGitHub != nil {
		if s := strings.TrimSpace(*rt.ActorGitHub); s != "" {
			return s
		}
	}
	return strings.TrimSpace(rt.ActorEmail)
}
