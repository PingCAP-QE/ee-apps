package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/google/go-github/v68/github"
	"github.com/rs/zerolog/log"
)

func runCommandCherryPickInvite(ctx context.Context, args []string) (string, error) {
	token := ctx.Value(ctxKeyGithubToken).(string)
	if len(args) < 2 {
		return "", fmt.Errorf("missing required positional arguments: pr_url, collaborator_username")
	}
	cherryPickPrUrl := args[0]
	collaboratorGithubID := args[1]

	gc := github.NewClient(nil).WithAuthToken(token)
	return cherryPickInvite(cherryPickPrUrl, collaboratorGithubID, gc)
}

func cherryPickInvite(prUrl string, collaboratorUsername string, gc *github.Client) (string, error) {
	u, err := url.Parse(prUrl)
	if err != nil {
		return "failure", fmt.Errorf("Failed to parse PR url: %v", err)
	}
	prPaths := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(prPaths) < 4 {
		return "failure", errors.New("Invalid PR url")
	}
	owner := prPaths[0]
	repo := prPaths[1]
	prNumber, _ := strconv.ParseInt(prPaths[3], 10, 32)

	pr, _, err := gc.PullRequests.Get(context.Background(), owner, repo, int(prNumber))
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
	_, res, err := gc.Repositories.AddCollaborator(context.Background(), pr.Head.Repo.Owner.GetLogin(), pr.Head.Repo.GetName(), collaboratorUsername, nil)
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
