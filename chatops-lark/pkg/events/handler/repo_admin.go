package handler

import (
	"context"
	"fmt"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/google/go-github/v68/github"
	"github.com/rs/zerolog/log"
)

const repoAdminHelpText = `missing required positional argument: repository_url

Usage: /repo-admins <owner/repo>

Description:
  Query repository administrators (excluding organization owners).
  This helps you find who to contact for repository write permissions.

Arguments:
  owner/repo The GitHub repository in format owner/repo

Examples:
  /repo-admins pingcap/tidb
  /repo-admins tikv/tikv

Note: Use owner/repo format only.

For more details, use: /repo-admins --help or /repo-admins -h
`

const repoAdminDetailedHelpText = `Usage: /repo-admins <owner/repo>

Description:
  Query repository administrators who can grant write permissions.

Examples:
  /repo-admins pingcap/tidb
  /repo-admins tikv/tikv

Required arguments:
  owner/repo  The GitHub repository in format owner/repo

Note: Use owner/repo format only.
`

func runCommandRepoAdmin(ctx context.Context, args []string) (string, error) {
	token, ok := ctx.Value(ctxKeyGithubToken).(string)
	if !ok || token == "" {
		return "", fmt.Errorf("GitHub token not found in context")
	}

	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		return repoAdminDetailedHelpText, NewInformationError("Requested command usage")
	}

	if len(args) != 1 {
		return "", fmt.Errorf(repoAdminHelpText)
	}

	gc := github.NewClient(nil).WithAuthToken(token)
	return queryRepoAdmins(ctx, args[0], gc)
}

func queryRepoAdmins(ctx context.Context, repo string, gc *github.Client) (string, error) {
	owner, repoName, err := parseRepo(repo)
	if err != nil {
		return "", err
	}
	log.Info().Str("owner", owner).Str("repo", repoName).Msg("Querying repository administrators")

	user, _, err := gc.Users.Get(ctx, owner)
	if err != nil {
		return "", fmt.Errorf("failed to get owner info: %w", err)
	}

	if user.GetType() == "Organization" {
		return getOrgAdmins(ctx, gc, owner, repoName)
	}

	return fmt.Sprintf("Repository administrator for `%s/%s`:\n\n1. %s\n\n→ Contact %s for write access",
		owner, repoName, owner, owner), nil
}

func parseRepo(repo string) (owner, repoName string, err error) {
	parts := strings.Split(repo, "/")
	if len(parts) != 2 {
		return "", "", fmt.Errorf("invalid repository format, expected owner/repo")
	}

	owner, repoName = parts[0], parts[1]
	if owner == "" || repoName == "" {
		return "", "", fmt.Errorf("owner and repo name cannot be empty")
	}

	return owner, repoName, nil
}

func getOrgAdmins(ctx context.Context, gc *github.Client, owner, repo string) (string, error) {
	collaborators, resp, err := gc.Repositories.ListCollaborators(ctx, owner, repo, &github.ListCollaboratorsOptions{
		Affiliation: "direct",
	})
	if err != nil {
		if resp != nil && resp.StatusCode == 404 {
			return "", fmt.Errorf("repository not found or no access permission")
		}
		return "", fmt.Errorf("failed to get collaborators: %w", err)
	}

	admins := extractAdmins(collaborators)

	// If no direct collaborators found and owner is organization, try organization teams
	if len(admins) == 0 {
		teamAdmins := getTeamAdmins(ctx, gc, owner, repo)
		admins = append(admins, teamAdmins...)
	}

	if len(admins) == 0 {
		return fmt.Sprintf("No repository administrators found for `%s/%s`.\n\n→ Contact repository owner for write access",
			owner, repo), nil
	}

	return formatAdminsResponse(owner, repo, admins), nil
}

func extractAdmins(collaborators []*github.User) []string {
	var admins []string
	for _, collab := range collaborators {
		if username := collab.GetLogin(); username != "" {
			if permissions := collab.GetPermissions(); permissions != nil && permissions["admin"] {
				admins = append(admins, username)
			}
		}
	}
	return admins
}

func getTeamAdmins(ctx context.Context, gc *github.Client, owner, repo string) []string {
	teams, _, err := gc.Repositories.ListTeams(ctx, owner, repo, nil)
	if err != nil {
		log.Warn().Err(err).Msg("Failed to get repository teams")
		return nil
	}

	org, _, err := gc.Organizations.Get(ctx, owner)
	if err != nil {
		log.Warn().Err(err).Str("owner", owner).Msg("Failed to get organization info")
		return nil
	}

	var admins []string
	for _, team := range teams {
		if team.GetPermission() == "admin" {
			members, _, err := gc.Teams.ListTeamMembersByID(ctx, org.GetID(), team.GetID(), nil)
			if err != nil {
				log.Warn().Err(err).Str("team", team.GetName()).Msg("Failed to get team members")
				continue
			}

			for _, member := range members {
				if username := member.GetLogin(); username != "" {
					admins = append(admins, username)
				}
			}
		}
	}

	return admins
}

func formatAdminsResponse(owner, repo string, admins []string) string {
	var result strings.Builder
	result.WriteString(fmt.Sprintf("Repository administrators for `%s/%s`:\n\n", owner, repo))

	for i, admin := range admins {
		result.WriteString(fmt.Sprintf("%d. @%s", i+1, admin))
		if i < len(admins)-1 {
			result.WriteString("\n")
		}
	}

	result.WriteString("\n\n→ Contact any admin above for write access")
	return result.String()
}

func setupCtxRepoAdmin(ctx context.Context, config config.Config, _ *CommandActor) context.Context {
	return context.WithValue(ctx, ctxKeyGithubToken, config.RepoAdmin.GithubToken)
}
