package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/rs/zerolog/log"
)

const cherryPickInviteHelpText = `Usage: /cherry-pick-invite <pr_url> <collaborator_username>

Description:
  Grants a collaborator permission to make changes to a cherry-pick PR.
  This allows the collaborator to help resolve conflicts or make necessary adjustments.

Examples:
  /cherry-pick-invite https://github.com/tikv/tikv/pull/12345 username123
  /cherry-pick-invite https://github.com/pingcap/tidb/pull/42123 username123

Required arguments:
  pr_url                 The URL of the cherry-pick pull request
  collaborator_username  The GitHub username of the collaborator to grant access

Use '/cherry-pick-invite --help' or '/cherry-pick-invite -h' to see this message.
`

// Permission sets requested from the token broker. Reading the pull request
// needs pull_requests: read on the base repository; inviting the collaborator
// needs administration: write, which the broker policy narrows to the
// ti-chi-bot fork repositories.
var (
	cherryPickReadPermissions   = map[string]string{"pull_requests": "read"}
	cherryPickInvitePermissions = map[string]string{"administration": "write"}
)

func runCommandCherryPickInvite(ctx context.Context, args []string) (string, error) {
	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return cherryPickInviteHelpText, NewInformationError("Requested command usage")
	}

	if len(args) < 2 {
		return "", errors.New(cherryPickInviteHelpText)
	}

	provider, err := gitHubClientFromContext(ctx)
	if err != nil {
		return "", err
	}

	return cherryPickInvite(ctx, args[0], args[1], provider)
}

// cherryPickInvite reads the cherry-pick pull request with a pull_requests-only
// token and invites the collaborator with a token scoped to the bot fork.
func cherryPickInvite(ctx context.Context, prUrl string, collaboratorUsername string, provider gitHubClientProvider) (string, error) {
	owner, repo, prNumber, err := parseCherryPickPRURL(prUrl)
	if err != nil {
		return "failure", err
	}

	prClient := provider.newClient(owner+"/"+repo, cherryPickReadPermissions)
	pr, _, err := prClient.PullRequests.Get(ctx, owner, repo, prNumber)
	if err != nil {
		return "failure", fmt.Errorf("Failed to get PR info: %v", err)
	}

	if pr.Head.Repo.FullName == nil {
		return "failure", errors.New("Invalid cherry-pick pr url, not found valid bot fork repo")
	}

	if !strings.HasPrefix(*pr.Head.Repo.FullName, "ti-chi-bot/") {
		return "failure", errors.New("Invalid PR, this is not a cherry-pick PR created by ti-chi-bot.")
	}

	botForkRepoFullName := *pr.Head.Repo.FullName
	inviteClient := provider.newClient(botForkRepoFullName, cherryPickInvitePermissions)
	_, res, err := inviteClient.Repositories.AddCollaborator(ctx, pr.Head.Repo.Owner.GetLogin(), pr.Head.Repo.GetName(), collaboratorUsername, nil)
	if err != nil {
		return "", fmt.Errorf("Failed to invite collaborator: %v", err)
	}
	switch res.StatusCode {
	case http.StatusOK, http.StatusCreated:
		return fmt.Sprintf("Successfully invited collaborator %s into repo %s. Please click https://github.com/%s/invitations to accept the invitation.(Invitations expire after 7 days)",
			collaboratorUsername, botForkRepoFullName, botForkRepoFullName), nil
	case http.StatusNoContent:
		return "", fmt.Errorf("User %s is already a collaborator of repo %s", collaboratorUsername, botForkRepoFullName)
	default:
		log.Error().Msgf("Failed to invite collaborator, status code: %d", res.StatusCode)
		return "", fmt.Errorf("Fail to invite collaborator, Please contact the EE team members for feedback.")
	}
}

func parseCherryPickPRURL(prUrl string) (owner, repo string, prNumber int, err error) {
	u, err := url.Parse(prUrl)
	if err != nil {
		return "", "", 0, fmt.Errorf("Failed to parse PR url: %v", err)
	}

	prPaths := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(prPaths) < 4 || prPaths[3] == "" {
		return "", "", 0, errors.New("Invalid PR url")
	}

	number, err := strconv.Atoi(prPaths[3])
	if err != nil || number <= 0 {
		return "", "", 0, errors.New("Invalid PR url")
	}

	return prPaths[0], prPaths[1], number, nil
}

func setupCtxCherryPickInvite(ctx context.Context, config config.Config, _ *CommandActor) context.Context {
	return withGitHubClientProvider(ctx, config, config.CherryPickInvite.GithubToken)
}
