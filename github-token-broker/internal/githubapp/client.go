package githubapp

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"time"

	"github.com/bradleyfalzon/ghinstallation/v2"
	"github.com/google/go-github/v92/github"
)

// ErrNoInstallation means the App is not installed for the requested repository.
var ErrNoInstallation = errors.New("github app is not installed for the repository")

// Token is a GitHub App installation access token.
type Token struct {
	Value          string
	ExpiresAt      time.Time
	InstallationID int64
}

// Minter mints installation tokens. permissions may be nil to request all
// permissions granted to the App (callers should normally narrow them).
type Minter interface {
	Mint(ctx context.Context, owner, repository string, repositories []string, permissions map[string]string) (Token, error)
}

// Client talks to the GitHub API using go-github and ghinstallation.
type Client struct {
	api *github.Client
}

// New creates a client from an App ID and a PEM private key file.
func New(appID int64, privateKeyPath, baseURL string, timeout time.Duration) (*Client, error) {
	if appID <= 0 {
		return nil, errors.New("app id must be positive")
	}
	if baseURL == "" {
		baseURL = "https://api.github.com"
	}
	baseURL = strings.TrimRight(baseURL, "/")

	transport, err := ghinstallation.NewAppsTransportKeyFromFile(http.DefaultTransport, appID, privateKeyPath)
	if err != nil {
		return nil, fmt.Errorf("create github app transport: %w", err)
	}
	transport.BaseURL = baseURL

	httpClient := &http.Client{Transport: transport}
	if timeout > 0 {
		httpClient.Timeout = timeout
	}
	api, err := github.NewClient(
		github.WithHTTPClient(httpClient),
		github.WithEnterpriseURLs(baseURL, baseURL),
	)
	if err != nil {
		return nil, fmt.Errorf("create github client: %w", err)
	}
	return &Client{api: api}, nil
}

// Mint resolves the App installation for the repository and creates an
// installation token, optionally narrowed to repositories and permissions.
func (c *Client) Mint(ctx context.Context, owner, repository string, repositories []string, permissions map[string]string) (Token, error) {
	installation, response, err := c.api.Apps.GetRepositoryInstallation(ctx, owner, repository)
	if err != nil {
		if isNotFound(response, err) {
			return Token{}, ErrNoInstallation
		}
		return Token{}, fmt.Errorf("find installation: %w", err)
	}
	installationID := installation.GetID()
	if installationID == 0 {
		return Token{}, errors.New("github returned an installation without an id")
	}

	permissionSet, err := permissionsToGitHub(permissions)
	if err != nil {
		return Token{}, err
	}
	token, _, err := c.api.Apps.CreateInstallationToken(ctx, installationID, &github.InstallationTokenOptions{
		Repositories: repositories,
		Permissions:  permissionSet,
	})
	if err != nil {
		return Token{}, fmt.Errorf("create installation token: %w", err)
	}
	if token.GetToken() == "" {
		return Token{}, errors.New("github returned an empty installation token")
	}
	return Token{
		Value:          token.GetToken(),
		ExpiresAt:      token.GetExpiresAt().Time,
		InstallationID: installationID,
	}, nil
}

func isNotFound(response *github.Response, err error) bool {
	if response != nil && response.StatusCode == http.StatusNotFound {
		return true
	}
	var errorResponse *github.ErrorResponse
	return errors.As(err, &errorResponse) && errorResponse.Response != nil && errorResponse.Response.StatusCode == http.StatusNotFound
}

// permissionsToGitHub maps permission names (the JSON names used by the GitHub
// API) onto go-github's typed InstallationPermissions struct.
func permissionsToGitHub(permissions map[string]string) (*github.InstallationPermissions, error) {
	if len(permissions) == 0 {
		return nil, nil
	}
	out := &github.InstallationPermissions{}
	value := reflect.ValueOf(out).Elem()
	fields := make(map[string]int, value.NumField())
	for i := 0; i < value.NumField(); i++ {
		tag := strings.Split(value.Type().Field(i).Tag.Get("json"), ",")[0]
		if tag != "" {
			fields[tag] = i
		}
	}
	for name, level := range permissions {
		index, ok := fields[name]
		if !ok {
			return nil, fmt.Errorf("unsupported permission %q", name)
		}
		value.Field(index).Set(reflect.ValueOf(&level))
	}
	return out, nil
}
