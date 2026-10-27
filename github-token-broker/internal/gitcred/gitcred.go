// Package gitcred implements a git credential helper that mints GitHub App
// installation tokens from the in-cluster token broker.
//
// The helper is meant to run inside a build pod: it authenticates to the broker
// with the pod ServiceAccount token and never handles the App private key.
package gitcred

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// defaultUsername is the placeholder user GitHub accepts with an
	// installation token in HTTP basic auth.
	defaultUsername = "x-access-token"

	// ModeApp and ModePAT are the values recorded in the state file so callers
	// can report which credential source was used for auditing.
	ModeApp = "app"
	ModePAT = "pat"

	defaultTimeout = 10 * time.Second
	defaultMargin  = 5 * time.Minute
	statePerm      = 0o600
	cacheDirPerm   = 0o700
)

var errEmptyToken = errors.New("broker returned an empty token")

// Config controls the credential helper.
type Config struct {
	// BrokerURL is the token broker base URL, for example
	// http://github-token-broker.github-token-broker.svc.
	BrokerURL string
	// TokenFile is the caller ServiceAccount token used to authenticate to the
	// broker.
	TokenFile string
	// CacheDir stores minted tokens between git invocations so long builds do
	// not mint on every fetch. Defaults to the OS cache directory.
	CacheDir string
	// Permissions is the permission set requested from the broker. Empty means
	// contents: read.
	Permissions map[string]string
	// Hosts lists the git hosts that may receive a token. Empty means
	// github.com only.
	Hosts []string
	// FallbackCredentialsFile is an optional .git-credentials file used when the
	// broker denies the request, so a repository outside the App installation
	// can still fall back to a PAT. Empty disables the fallback.
	FallbackCredentialsFile string
	// StateFile records the last successful credential source (app or pat).
	StateFile string
	// Timeout bounds a single broker request.
	Timeout time.Duration
	// Margin is how long before expiry a cached token is refreshed.
	Margin time.Duration
	// Now is the clock, overridable in tests.
	Now func() time.Time
	// Client is the HTTP client used for broker requests, overridable in tests.
	Client *http.Client
	// Logger receives warnings about the fallback path. Defaults to a logger
	// writing to os.Stderr.
	Logger *slog.Logger
}

// Helper mints and caches installation tokens for git credential requests.
type Helper struct {
	cfg    Config
	logger *slog.Logger

	mu   sync.Mutex
	mode string
}

// Request is the subset of the git credential request that the helper needs.
type Request struct {
	Protocol string
	Host     string
	Path     string
}

// Response is the git credential response.
type Response struct {
	Username string
	Password string
}

// New validates the configuration and prepares the cache directory.
func New(cfg Config) (*Helper, error) {
	if strings.TrimSpace(cfg.BrokerURL) == "" {
		return nil, errors.New("gitcred: broker URL is required")
	}
	if strings.TrimSpace(cfg.TokenFile) == "" {
		return nil, errors.New("gitcred: caller token file is required")
	}
	cfg.BrokerURL = strings.TrimRight(cfg.BrokerURL, "/")
	if len(cfg.Permissions) == 0 {
		cfg.Permissions = map[string]string{"contents": "read"}
	}
	if len(cfg.Hosts) == 0 {
		cfg.Hosts = []string{"github.com"}
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = defaultTimeout
	}
	if cfg.Margin <= 0 {
		cfg.Margin = defaultMargin
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.CacheDir == "" {
		cfg.CacheDir = filepath.Join(os.TempDir(), "git-credential-broker")
	}
	if cfg.Client == nil {
		cfg.Client = &http.Client{Timeout: cfg.Timeout}
	}
	logger := cfg.Logger
	if logger == nil {
		logger = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn}))
	}
	if err := os.MkdirAll(cfg.CacheDir, cacheDirPerm); err != nil {
		return nil, fmt.Errorf("gitcred: create cache directory: %w", err)
	}
	return &Helper{cfg: cfg, logger: logger}, nil
}

// AuthMode reports the credential source of the last successful Get.
func (h *Helper) AuthMode() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.mode
}

// Get returns credentials for the request. It returns an empty Response, and no
// error, when the request is out of scope (another host or protocol, or a URL
// without an owner/repository pair) so git can try the next helper.
func (h *Helper) Get(ctx context.Context, req Request) (Response, error) {
	if !h.inScope(req) {
		return Response{}, nil
	}
	_, repo, err := repoFromPath(req.Path)
	if err != nil {
		return Response{}, nil
	}
	token, err := h.token(ctx, repo)
	if err != nil {
		if resp, ok := h.fallback(req.Host); ok {
			h.logger.Warn("token broker denied git credentials, using fallback PAT", "repo", repo, "error", err)
			h.setState(ModePAT)
			return resp, nil
		}
		return Response{}, err
	}
	h.setState(ModeApp)
	return Response{Username: defaultUsername, Password: token}, nil
}

// Erase drops any cached token for the request, so a rejected credential is
// minted again on the next attempt.
func (h *Helper) Erase(req Request) error {
	_, repo, err := repoFromPath(req.Path)
	if err != nil {
		return nil
	}
	path := h.cachePath(repo)
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("gitcred: remove cached token: %w", err)
	}
	return nil
}

func (h *Helper) inScope(req Request) bool {
	if !strings.EqualFold(req.Protocol, "https") {
		return false
	}
	for _, host := range h.cfg.Hosts {
		if strings.EqualFold(host, req.Host) {
			return true
		}
	}
	return false
}

func (h *Helper) token(ctx context.Context, repo string) (string, error) {
	now := h.cfg.Now()
	if cached, ok := h.loadCached(repo); ok && cached.ExpiresAt.After(now.Add(h.cfg.Margin)) {
		return cached.Token, nil
	}
	minted, err := h.mint(ctx, repo)
	if err != nil {
		return "", err
	}
	h.storeCached(repo, minted)
	return minted.Token, nil
}

type cachedToken struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

func (h *Helper) cachePath(repo string) string {
	key := strings.ToLower(repo) + "|" + permissionKey(h.cfg.Permissions)
	sum := sha256.Sum256([]byte(key))
	return filepath.Join(h.cfg.CacheDir, hex.EncodeToString(sum[:16])+".json")
}

func (h *Helper) loadCached(repo string) (cachedToken, bool) {
	data, err := os.ReadFile(h.cachePath(repo))
	if err != nil {
		return cachedToken{}, false
	}
	var cached cachedToken
	if err := json.Unmarshal(data, &cached); err != nil || cached.Token == "" {
		return cachedToken{}, false
	}
	return cached, true
}

func (h *Helper) storeCached(repo string, token cachedToken) {
	data, err := json.Marshal(token)
	if err != nil {
		return
	}
	tmp, err := os.CreateTemp(h.cfg.CacheDir, ".token-*")
	if err != nil {
		return
	}
	name := tmp.Name()
	defer func() { _ = os.Remove(name) }()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return
	}
	if err := tmp.Close(); err != nil {
		return
	}
	if err := os.Chmod(name, statePerm); err != nil {
		return
	}
	_ = os.Rename(name, h.cachePath(repo))
}

type tokenPayload struct {
	Repo        string            `json:"repo"`
	Permissions map[string]string `json:"permissions"`
}

type tokenResponse struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

func (h *Helper) mint(ctx context.Context, repo string) (cachedToken, error) {
	caller, err := os.ReadFile(h.cfg.TokenFile)
	if err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: read caller token: %w", err)
	}
	callerToken := strings.TrimSpace(string(caller))
	if callerToken == "" {
		return cachedToken{}, errors.New("gitcred: caller token is empty")
	}
	body, err := json.Marshal(tokenPayload{Repo: repo, Permissions: h.cfg.Permissions})
	if err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: encode broker request: %w", err)
	}

	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return cachedToken{}, ctx.Err()
			case <-time.After(200 * time.Millisecond):
			}
		}
		token, err := h.mintOnce(ctx, callerToken, body)
		if err == nil {
			return token, nil
		}
		lastErr = err
		if !retryable(err) {
			break
		}
	}
	return cachedToken{}, lastErr
}

func (h *Helper) mintOnce(ctx context.Context, callerToken string, body []byte) (cachedToken, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.cfg.BrokerURL+"/v1/token", strings.NewReader(string(body)))
	if err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: build broker request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+callerToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.cfg.Client.Do(req)
	if err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: call token broker: %w", &brokerError{err: err, retry: true})
	}
	defer func() { _ = resp.Body.Close() }()

	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: read broker response: %w", &brokerError{err: err, retry: true})
	}
	if resp.StatusCode != http.StatusOK {
		return cachedToken{}, fmt.Errorf("gitcred: %w", &brokerError{
			err:   fmt.Errorf("token broker returned HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(payload))),
			retry: resp.StatusCode >= 500,
		})
	}
	var out tokenResponse
	if err := json.Unmarshal(payload, &out); err != nil {
		return cachedToken{}, fmt.Errorf("gitcred: decode broker response: %w", err)
	}
	if strings.TrimSpace(out.Token) == "" {
		return cachedToken{}, errEmptyToken
	}
	return cachedToken{Token: out.Token, ExpiresAt: out.ExpiresAt}, nil
}

// brokerError carries the retry decision without exposing token material.
type brokerError struct {
	err   error
	retry bool
}

func (e *brokerError) Error() string { return e.err.Error() }
func (e *brokerError) Unwrap() error { return e.err }

func retryable(err error) bool {
	var be *brokerError
	return errors.As(err, &be) && be.retry
}

func (h *Helper) fallback(host string) (Response, bool) {
	if h.cfg.FallbackCredentialsFile == "" {
		return Response{}, false
	}
	file, err := os.Open(h.cfg.FallbackCredentialsFile)
	if err != nil {
		return Response{}, false
	}
	defer func() { _ = file.Close() }()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		parsed, err := url.Parse(line)
		if err != nil || !strings.EqualFold(parsed.Hostname(), host) {
			continue
		}
		if parsed.User == nil {
			continue
		}
		password, _ := parsed.User.Password()
		if password == "" {
			continue
		}
		return Response{Username: parsed.User.Username(), Password: password}, true
	}
	return Response{}, false
}

func (h *Helper) setState(mode string) {
	h.mu.Lock()
	h.mode = mode
	h.mu.Unlock()
	if h.cfg.StateFile == "" {
		return
	}
	if err := os.WriteFile(h.cfg.StateFile, []byte(mode+"\n"), statePerm); err != nil {
		h.logger.Warn("cannot record git auth mode", "path", h.cfg.StateFile, "error", err)
	}
}

// ParseRequest reads the git credential request attributes.
func ParseRequest(r io.Reader) (Request, error) {
	var req Request
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r")
		if line == "" {
			break
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch key {
		case "protocol":
			req.Protocol = value
		case "host":
			req.Host = value
		case "path":
			req.Path = strings.TrimPrefix(value, "/")
		}
	}
	if err := scanner.Err(); err != nil {
		return Request{}, err
	}
	return req, nil
}

// Write renders the credential response in the git credential protocol format.
func (r Response) Write(w io.Writer) error {
	if r.Password == "" {
		return nil
	}
	// The git credential protocol terminates a helper response with a blank
	// line (git's own store helper does the same).
	if _, err := fmt.Fprintf(w, "username=%s\npassword=%s\n\n", r.Username, r.Password); err != nil {
		return err
	}
	return nil
}

// repoFromPath extracts owner/repository from a git credential path.
func repoFromPath(path string) (string, string, error) {
	cleaned := strings.Trim(path, "/")
	cleaned = strings.TrimSuffix(cleaned, ".git")
	parts := strings.Split(cleaned, "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("gitcred: cannot derive owner/repository from %q", path)
	}
	owner := parts[0]
	return owner, owner + "/" + parts[1], nil
}

func permissionKey(permissions map[string]string) string {
	names := make([]string, 0, len(permissions))
	for name := range permissions {
		names = append(names, name)
	}
	sort.Strings(names)
	var b strings.Builder
	for _, name := range names {
		b.WriteString(name)
		b.WriteString("=")
		b.WriteString(permissions[name])
		b.WriteString(";")
	}
	return b.String()
}
