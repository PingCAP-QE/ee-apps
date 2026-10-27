package handler

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/google/go-github/v68/github"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/client/githubauth"
)

const (
	// defaultGitHubBrokerURL is the in-cluster token broker service.
	defaultGitHubBrokerURL = "http://github-token-broker.github-token-broker.svc"
	// defaultGitHubBrokerTokenFile is the projected ServiceAccount token.
	defaultGitHubBrokerTokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"

	envGitHubBrokerURL       = "GITHUB_TOKEN_BROKER_URL"
	envGitHubBrokerTokenFile = "GITHUB_TOKEN_BROKER_TOKEN_FILE"

	githubAPIRequestTimeout = 30 * time.Second
)

// Permission sets requested from the token broker. Each command asks only for
// what its GitHub API calls need.
var (
	// repoAdminPermissions covers listing org members (members) and repository
	// collaborators (administration). Listing collaborators is not covered by
	// members: read; GitHub answers 403 without administration: read.
	repoAdminPermissions = map[string]string{
		"members":        "read",
		"administration": "read",
	}
	// registryImagePermissions covers workflow dispatch, run polling, job logs
	// and artifact downloads for /cloud-image.
	registryImagePermissions = map[string]string{"actions": "write"}
)

// gitHubClientProvider builds GitHub API clients scoped to one repository and
// one permission set.
type gitHubClientProvider interface {
	newClient(repo string, permissions map[string]string) *github.Client
}

// githubClientFactory builds GitHub clients that prefer installation tokens
// minted by the in-cluster token broker and fall back to a static token when
// one is explicitly configured.
type githubClientFactory struct {
	brokerURL     string
	tokenFile     string
	fallbackToken string
}

// newGitHubClientFactory resolves the broker settings for a command. The
// broker is only used when the `github` config block is present or
// GITHUB_TOKEN_BROKER_URL is set; otherwise the command keeps working with the
// legacy static token alone.
func newGitHubClientFactory(cfg config.Config, legacyToken string) githubClientFactory {
	factory := githubClientFactory{fallbackToken: strings.TrimSpace(legacyToken)}

	if cfg.GitHub != nil {
		factory.brokerURL = strings.TrimSpace(cfg.GitHub.BrokerURL)
		if factory.brokerURL == "" {
			factory.brokerURL = defaultGitHubBrokerURL
		}
		factory.tokenFile = strings.TrimSpace(cfg.GitHub.BrokerTokenFile)
		if factory.fallbackToken == "" {
			factory.fallbackToken = strings.TrimSpace(cfg.GitHub.Token)
		}
	}

	if url := strings.TrimSpace(os.Getenv(envGitHubBrokerURL)); url != "" {
		factory.brokerURL = url
	}
	if path := strings.TrimSpace(os.Getenv(envGitHubBrokerTokenFile)); path != "" {
		factory.tokenFile = path
	}
	if factory.tokenFile == "" {
		factory.tokenFile = defaultGitHubBrokerTokenFile
	}

	return factory
}

// transportConfig assembles the broker transport for one repository scope.
func (f githubClientFactory) transportConfig(repo string, permissions map[string]string) githubauth.Config {
	return githubauth.Config{
		BrokerURL:     f.brokerURL,
		TokenFile:     f.tokenFile,
		FallbackToken: f.fallbackToken,
		Repo:          repo,
		Permissions:   permissions,
		OnFallback: func(repo string, err error) {
			// A fallback means the command ran with a static PAT (or without a
			// token at all). Never let that happen silently.
			log.Warn().
				Err(err).
				Str("repo", repo).
				Str("token_source", "fallback").
				Msg("GitHub token broker request failed; falling back to the static GitHub token")
		},
	}
}

// newClient returns a GitHub client whose requests carry a token scoped to
// repo and permissions.
func (f githubClientFactory) newClient(repo string, permissions map[string]string) *github.Client {
	httpClient := &http.Client{
		Transport: githubauth.NewTransport(f.transportConfig(repo, permissions)),
		Timeout:   githubAPIRequestTimeout,
	}
	return github.NewClient(httpClient)
}

// withGitHubClientProvider stores a client provider in the command context.
func withGitHubClientProvider(ctx context.Context, cfg config.Config, legacyToken string) context.Context {
	return context.WithValue(ctx, ctxKeyGitHubClient, gitHubClientProvider(newGitHubClientFactory(cfg, legacyToken)))
}

// gitHubClientFromContext returns the client provider set up for the command.
func gitHubClientFromContext(ctx context.Context) (gitHubClientProvider, error) {
	provider, ok := ctx.Value(ctxKeyGitHubClient).(gitHubClientProvider)
	if !ok || provider == nil {
		return nil, errors.New("GitHub client provider not found in context")
	}
	return provider, nil
}
