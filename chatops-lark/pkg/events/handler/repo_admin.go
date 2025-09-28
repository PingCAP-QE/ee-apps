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

Usage: /repo-admins <repository_url>

Description:
  Query repository administrators (excluding organization owners).
  This helps you find who to contact for repository write permissions.

Arguments:
  repository_url The GitHub repository URL (e.g., https://github.com/pingcap/tidb)

Examples:
  /repo-admins https://github.com/pingcap/tidb
  /repo-admins https://github.com/tikv/tikv.git

Note: This command excludes organization owners and focuses on repository-specific administrators.
`

func runCommandRepoAdmin(ctx context.Context, args []string) (string, error) {
	token := ctx.Value(ctxKeyGithubToken).(string)
	if len(args) < 1 {
		return "", fmt.Errorf(repoAdminHelpText)
	}

	repoURL := args[0]
	gc := github.NewClient(nil).WithAuthToken(token)
	return queryRepoAdmins(repoURL, gc)
}

func queryRepoAdmins(repoURL string, gc *github.Client) (string, error) {
	// Parse repository URL
	owner, repo, err := parseRepoURL(repoURL)
	if err != nil {
		return "", err
	}

	log.Info().
		Str("owner", owner).
		Str("repo", repo).
		Msg("Querying repository administrators")

	// Get repository direct collaborators
	collaborators, resp, err := gc.Repositories.ListCollaborators(context.Background(), owner, repo, &github.ListCollaboratorsOptions{
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

		perm, _, err := gc.Repositories.GetPermissionLevel(context.Background(), owner, repo, username)
		if err != nil {
			log.Warn().Err(err).Str("user", username).Msg("Failed to get permission level")
			continue
		}

		permission := perm.GetPermission()
		if permission == "admin" {
			if !adminMap[username] {
				admins = append(admins, username)
				adminMap[username] = true
			}
		}
	}

	// If there have no repository direct collaborators, get organization members with admin role (excluding owners)
	if len(admins) == 0 {
		members, _, err := gc.Organizations.ListMembers(context.Background(), owner, &github.ListMembersOptions{
			Role: "admin",
		})
		if err != nil {
			log.Warn().Err(err).Msg("Failed to get organization admins")
		} else {
			// Filter out organization owners
			for _, member := range members {
				username := member.GetLogin()
				if username == "" || adminMap[username] {
					continue
				}

				isOwner, err := isOrganizationOwner(gc, owner, username)
				if err != nil {
					log.Warn().Err(err).Str("user", username).Msg("Failed to check if user is owner")
					continue
				}

				if !isOwner {
					admins = append(admins, username)
					adminMap[username] = true
				}
			}
		}
	}

	if len(admins) == 0 {
		return fmt.Sprintf("No repository administrators found for `%s/%s`.\n\nNote: This excludes organization owners. If you need write access, please contact the organization owners directly.", owner, repo), nil
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

func isOrganizationOwner(gc *github.Client, org, username string) (bool, error) {
	member, _, err := gc.Organizations.GetOrgMembership(context.Background(), username, org)
	if err != nil {
		return false, err
	}

	return member.GetRole() == "admin" && member.GetState() == "active", nil
}

func setupCtxRepoAdmin(ctx context.Context, config config.Config, _ *CommandActor) context.Context {
	return context.WithValue(ctx, ctxKeyGithubToken, config.RepoAdmin.GithubToken)
}
