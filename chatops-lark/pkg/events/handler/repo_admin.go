package handler

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/google/go-github/v68/github"
	"github.com/rs/zerolog/log"
)

const repoAdminHelpText = `Usage: /repo-admins <owner/repo>

Description:
  Query repository administrators who can grant write permissions.
  This command helps you identify the right people to contact when you need
  write access to a GitHub repository.

Examples:
  /repo-admins pingcap/tidb
  /repo-admins tikv/tikv

Required arguments:
  owner/repo  The GitHub repository in format owner/repo

Note: This command excludes organization owners and shows repository-specific
      administrators who can grant you write access.

Use '/repo-admins --help' or '/repo-admins -h' to see this message.
`

const (
	repoAdminFeatureFlag = "ALPHA"
)

func runCommandRepoAdmin(ctx context.Context, args []string) (string, error) {
	token, ok := ctx.Value(ctxKeyGithubToken).(string)
	if !ok || token == "" {
		return "", fmt.Errorf("GitHub token not found in context")
	}

	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return repoAdminHelpText, NewInformationError("Requested command usage")
	}

	if len(args) != 1 {
		return "", errors.New(repoAdminHelpText)
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
		return "", "", errors.New("invalid repository format, expected owner/repo")
	}

	owner, repoName = parts[0], parts[1]
	if owner == "" || repoName == "" {
		return "", "", errors.New("owner and repo name cannot be empty")
	}

	return owner, repoName, nil
}

func getNoAdminsMessage(owner, repo string) string {
	return fmt.Sprintf("No repository administrators found for `%s/%s`.\n\n→ Contact repository owner for write access",
		owner, repo)
}

func getOrgAdmins(ctx context.Context, gc *github.Client, owner, repo string) (string, error) {
	orgOwners, err := getOrgOwners(ctx, gc, owner)
	if err != nil {
		log.Warn().Err(err).Msg("Failed to get org owners")
	}

	collaborators, resp, err := gc.Repositories.ListCollaborators(ctx, owner, repo, &github.ListCollaboratorsOptions{
		Affiliation: "direct", // retrieve only direct collaborators, excluding those with access via the organization.
		Permission:  "admin",
	})
	if err != nil {
		if resp != nil && resp.StatusCode == 404 {
			return "", errors.New("repository not found or no access permission")
		}
		return "", fmt.Errorf("failed to get collaborators: %w", err)
	}

	// 1. if direct repository administrators is empty, return org owners
	if len(collaborators) == 0 {
		if len(orgOwners) > 0 {
			return formatAdminsResponse(owner, repo, orgOwners), nil
		}
		return getNoAdminsMessage(owner, repo), nil
	}

	ownerMap := make(map[string]bool, len(orgOwners))
	for _, ownerName := range orgOwners {
		ownerMap[ownerName] = true
	}

	var repoSpecificHumanAdmins []string
	var allDirectHumanAdmins []string

	for _, collab := range collaborators {
		username := collab.GetLogin()
		if username == "" {
			continue
		}

		if !isBot(username) {
			allDirectHumanAdmins = append(allDirectHumanAdmins, username)
			if !ownerMap[username] {
				repoSpecificHumanAdmins = append(repoSpecificHumanAdmins, username)
			}
		}
	}

	// 2. return direct repository administrators that not include someone who is also an org owner
	if len(repoSpecificHumanAdmins) > 0 {
		return formatAdminsResponse(owner, repo, repoSpecificHumanAdmins), nil
	}

	// 3. if the direct repository administrators are empty after filtering out the org owner, return the direct repository administrators
	if len(allDirectHumanAdmins) > 0 {
		return formatAdminsResponse(owner, repo, allDirectHumanAdmins), nil
	}

	// 4. if there are no direct repository administrators or only bot, return the org owners
	if len(orgOwners) > 0 {
		return formatAdminsResponse(owner, repo, orgOwners), nil
	}

	return getNoAdminsMessage(owner, repo), nil
}

func isBot(username string) bool {
	usernameLower := strings.ToLower(username)
	botRegex := regexp.MustCompile(`^(.+-)?bot(-.+)?$`)
	if botRegex.MatchString(usernameLower) {
		return true
	}

	return false
}

func getOrgOwners(ctx context.Context, gc *github.Client, org string) ([]string, error) {
	members, _, err := gc.Organizations.ListMembers(ctx, org, &github.ListMembersOptions{
		Role: "admin",
	})
	if err != nil {
		return nil, err
	}

	owners := make([]string, 0, len(members))
	for _, member := range members {
		if username := member.GetLogin(); username != "" {
			owners = append(owners, username)
		}
	}
	return owners, nil
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

	result.WriteString("\n\n→ Contact any contact whose GitHub ID is in the above list")

	if repoAdminFeatureFlag != "" {
		result.WriteString(fmt.Sprintf("\n\n⚠️  This is a %s feature. Please report any issues or feedback to EE ChatOps bot.", repoAdminFeatureFlag))
	}

	return result.String()
}

func setupCtxRepoAdmin(ctx context.Context, config config.Config, _ *CommandActor) context.Context {
	return context.WithValue(ctx, ctxKeyGithubToken, config.RepoAdmin.GithubToken)
}
