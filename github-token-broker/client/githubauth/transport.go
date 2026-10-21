// Package githubauth provides an HTTP transport that mints repository-scoped
// GitHub App installation tokens from the in-cluster token broker.
package githubauth

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	defaultTokenFile     = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	defaultBrokerTimeout = 10 * time.Second
	tokenSafetyMargin    = 5 * time.Minute
)

// Config controls the broker-backed transport.
type Config struct {
	// BrokerURL is the base URL of the token broker. Empty disables the broker
	// and makes the transport use FallbackToken.
	BrokerURL string
	// TokenFile contains the caller ServiceAccount token. Defaults to the
	// in-cluster projected token path.
	TokenFile string
	// FallbackToken is used when the broker is disabled, the request is not
	// repository-scoped, or the broker denies the request. It may be empty.
	FallbackToken string
	// Base is the transport used for GitHub API requests. Broker requests use
	// the default transport.
	Base http.RoundTripper
}

// Transport adds a repository-scoped GitHub token to outgoing API requests.
type Transport struct {
	brokerURL     string
	tokenFile     string
	fallbackToken string
	base          http.RoundTripper
	brokerClient  *http.Client

	mu    sync.Mutex
	cache map[cacheKey]cachedToken
}

type cacheKey struct {
	repo         string
	contents     string
	pullRequests string
}

type cachedToken struct {
	value      string
	validUntil time.Time
}

type brokerResponse struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

// NewTransport returns a transport configured from cfg.
func NewTransport(cfg Config) *Transport {
	base := cfg.Base
	if base == nil {
		base = http.DefaultTransport
	}
	tokenFile := cfg.TokenFile
	if tokenFile == "" {
		tokenFile = defaultTokenFile
	}
	return &Transport{
		brokerURL:     strings.TrimRight(cfg.BrokerURL, "/"),
		tokenFile:     tokenFile,
		fallbackToken: cfg.FallbackToken,
		base:          base,
		brokerClient: &http.Client{
			Timeout:   defaultBrokerTimeout,
			Transport: http.DefaultTransport,
		},
		cache: map[cacheKey]cachedToken{},
	}
}

// RoundTrip implements http.RoundTripper.
func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	repo, scoped := repositoryFromPath(req.URL.Path)
	perms := permissionsForMethod(req.Method)
	token, err := t.tokenFor(req.Context(), req, repo, scoped, perms)
	if err != nil {
		return nil, err
	}

	out := req.Clone(req.Context())
	if token != "" && out.Header.Get("Authorization") == "" {
		out.Header.Set("Authorization", "Bearer "+token)
	}
	return t.base.RoundTrip(out)
}

func (t *Transport) tokenFor(ctx context.Context, req *http.Request, repo string, scoped bool, perms map[string]string) (string, error) {
	if t.brokerURL == "" || !scoped || !strings.EqualFold(req.URL.Hostname(), "api.github.com") {
		return t.fallbackToken, nil
	}

	key := cacheKey{
		repo:         repo,
		contents:     perms["contents"],
		pullRequests: perms["pull_requests"],
	}
	now := time.Now()
	t.mu.Lock()
	cached, ok := t.cache[key]
	t.mu.Unlock()
	if ok && now.Before(cached.validUntil) {
		return cached.value, nil
	}

	token, expiresAt, err := t.mint(ctx, repo, perms)
	if err != nil {
		if t.fallbackToken != "" {
			return t.fallbackToken, nil
		}
		return "", err
	}
	if expiresAt.After(now.Add(tokenSafetyMargin)) {
		t.mu.Lock()
		t.cache[key] = cachedToken{value: token, validUntil: expiresAt.Add(-tokenSafetyMargin)}
		t.mu.Unlock()
	}
	return token, nil
}

func (t *Transport) mint(ctx context.Context, repo string, perms map[string]string) (string, time.Time, error) {
	saToken, err := os.ReadFile(t.tokenFile)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("read broker caller token: %w", err)
	}
	caller := strings.TrimSpace(string(saToken))
	if caller == "" {
		return "", time.Time{}, fmt.Errorf("broker caller token is empty")
	}

	payload, err := json.Marshal(map[string]any{
		"repo":        repo,
		"permissions": perms,
	})
	if err != nil {
		return "", time.Time{}, fmt.Errorf("encode broker request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.brokerURL+"/v1/token", bytes.NewReader(payload))
	if err != nil {
		return "", time.Time{}, fmt.Errorf("build broker request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+caller)
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.brokerClient.Do(req)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("call token broker: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", time.Time{}, fmt.Errorf("read broker response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return "", time.Time{}, fmt.Errorf("token broker returned HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var out brokerResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return "", time.Time{}, fmt.Errorf("decode broker response: %w", err)
	}
	if out.Token == "" {
		return "", time.Time{}, fmt.Errorf("token broker returned an empty token")
	}
	if out.ExpiresAt.IsZero() {
		out.ExpiresAt = time.Now().Add(50 * time.Minute)
	}
	return out.Token, out.ExpiresAt, nil
}

// repositoryFromPath extracts owner/repo from /repos/{owner}/{repo}/... paths.
func repositoryFromPath(path string) (string, bool) {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) < 3 || parts[0] != "repos" || parts[1] == "" || parts[2] == "" {
		return "", false
	}
	return parts[1] + "/" + parts[2], true
}

func permissionsForMethod(method string) map[string]string {
	switch method {
	case http.MethodGet, http.MethodHead:
		return map[string]string{
			"contents":      "read",
			"pull_requests": "read",
		}
	default:
		return map[string]string{
			"contents":      "write",
			"pull_requests": "write",
		}
	}
}
