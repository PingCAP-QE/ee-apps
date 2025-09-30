package handler

import (
	"context"
	"fmt"
	"net/url"
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

Note: This command excludes organization owners and focuses on repository-specific administrators.
`

func runCommandRepoAdmin(ctx context.Context, args []string) (string, error) {
	token, ok := ctx.Value(ctxKeyGithubToken).(string)
	if !ok || token == "" {
		return "", fmt.Errorf("GitHub token not found in context")
	}

	if token == "" {
		return "", fmt.Errorf("GitHub token is empty")
	}

	if len(args) < 1 {
		return "", fmt.Errorf(repoAdminHelpText)
	}

	repoURL := args[0]
	gc := github.NewClient(nil).WithAuthToken(token)
	return queryRepoAdmins(ctx, repoURL, gc)
}

func queryRepoAdmins(ctx context.Context, repoURL string, gc *github.Client) (string, error) {
	// Parse repository URL
	owner, repo, err := parseRepoURL(repoURL)
	if err != nil {
		return "", err
	}

	log.Info().
		Str("owner", owner).
		Str("repo", repo).
		Msg("Querying repository administrators")

	// Check if owner is an organization first
	isOrg, err := isOrganization(ctx, gc, owner)
	if err != nil {
		log.Warn().Err(err).Str("owner", owner).Msg("Failed to check if owner is organization")
		// Continue with direct collaborators only
		isOrg = false
	}

	// Get repository direct collaborators
	collaborators, resp, err := gc.Repositories.ListCollaborators(ctx, owner, repo, &github.ListCollaboratorsOptions{
		Affiliation: "direct",
	})
	if err != nil {
		if resp != nil && resp.StatusCode == 404 {
			return "", fmt.Errorf("repository not found or no access permission")
		}
		return "", fmt.Errorf("failed to get repository collaborators: %v", err)
	}

	var admins []string
	adminMap := make(map[string]bool)

	for _, collab := range collaborators {
		username := collab.GetLogin()
		if username == "" {
			continue
		}

		// Use permissions from collaborator object (avoid N+1 query)
		permissions := collab.GetPermissions()
		if permissions != nil && permissions["admin"] {
			if !adminMap[username] {
				admins = append(admins, username)
				adminMap[username] = true
			}
		}
	}

	// If no direct collaborators found and owner is organization, try organization teams
	if len(admins) == 0 && isOrg {
		teamAdmins := getTeamAdmins(ctx, gc, owner, repo)
		for _, admin := range teamAdmins {
			if !adminMap[admin] {
				admins = append(admins, admin)
				adminMap[admin] = true
			}
		}
	}

	if len(admins) == 0 {
		return fmt.Sprintf("No repository administrators found for `%s/%s`.\n\n→ Contact repository owner for write access", owner, repo), nil
	}

	// Format the response
	var result strings.Builder
	result.WriteString(fmt.Sprintf("Repository administrators for `%s/%s`:\n\n", owner, repo))

	for i, admin := range admins {
		result.WriteString(fmt.Sprintf("%d. @%s", i+1, admin))
		if i < len(admins)-1 {
			result.WriteString("\n")
		}
	}

	result.WriteString("\n\n→ Contact any admin above for write access")

	return result.String(), nil
}

func parseRepoURL(repoURL string) (owner, repo string, err error) {
	if !strings.Contains(repoURL, "/") && !strings.Contains(repoURL, "://") && !strings.HasPrefix(repoURL, "git@") {
		return "", "", fmt.Errorf("invalid repository format, expected owner/repo or full URL")
	}

	if !strings.Contains(repoURL, "://") && !strings.HasPrefix(repoURL, "git@") && strings.Count(repoURL, "/") == 1 {
		parts := strings.Split(repoURL, "/")
		if len(parts) != 2 {
			return "", "", fmt.Errorf("invalid owner/repo format")
		}
		owner = parts[0]
		repo = strings.TrimSuffix(parts[1], ".git")
		return owner, repo, nil
	}

	// deal with ssh urls, e.g. git@github.com:owner/repo.git
	if strings.HasPrefix(repoURL, "git@") && strings.Contains(repoURL, ":") {
		parts := strings.Split(repoURL, ":")
		if len(parts) != 2 {
			return "", "", fmt.Errorf("invalid SSH URL format")
		}

		path := strings.TrimPrefix(parts[1], "/")
		pathParts := strings.Split(path, "/")
		if len(pathParts) < 2 {
			return "", "", fmt.Errorf("invalid repository path in SSH URL")
		}

		owner = pathParts[0]
		repo = strings.TrimSuffix(pathParts[1], ".git")
		return owner, repo, nil
	}

	// deal with path like github.com/owner/repo without https://
	if !strings.Contains(repoURL, "://") && strings.Contains(repoURL, "/") {
		repoURL = "https://" + repoURL
	}

	u, err := url.Parse(repoURL)
	if err != nil {
		return "", "", fmt.Errorf("failed to parse repository URL: %v", err)
	}

	pathParts := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(pathParts) < 2 {
		return "", "", fmt.Errorf("invalid repository URL format")
	}

	owner = pathParts[0]
	repo = strings.TrimSuffix(pathParts[1], ".git")
	return owner, repo, nil
}

func isOrganization(ctx context.Context, gc *github.Client, owner string) (bool, error) {
	user, _, err := gc.Users.Get(ctx, owner)
	if err != nil {
		return false, err
	}
	return user.GetType() == "Organization", nil
}

func getTeamAdmins(ctx context.Context, gc *github.Client, owner, repo string) []string {
	var allAdmins []string

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
	orgID := org.GetID()

	for _, team := range teams {
		if team.GetPermission() == "admin" {
			members, _, err := gc.Teams.ListTeamMembersByID(ctx, orgID, team.GetID(), nil)
			if err != nil {
				log.Warn().Err(err).Str("team", team.GetName()).Msg("Failed to get team members")
				continue
			}

			for _, member := range members {
				username := member.GetLogin()
				if username != "" {
					allAdmins = append(allAdmins, username)
				}
			}
		}
	}

	return allAdmins
}

func setupCtxRepoAdmin(ctx context.Context, config config.Config, _ *CommandActor) context.Context {
	return context.WithValue(ctx, ctxKeyGithubToken, config.RepoAdmin.GithubToken)
}
