package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Verifier checks Gate 1: that a Directory user id equals the ID token `sub` we
// see in a decision log. Tuples are written as user:google/sub/<directory id>, so
// a mismatch would silently authorize the wrong person — the check exists to be
// run once, with a real credential, before the first production sync.
type Verifier struct {
	baseURL string
	token   func(context.Context) (string, error)
	client  *http.Client
}

// VerifierConfig mirrors the parts of Config a verification needs.
type VerifierConfig struct {
	BaseURL    string
	Token      func(context.Context) (string, error)
	HTTPClient *http.Client
	Timeout    time.Duration
}

// NewVerifier validates the endpoint and credential source.
func NewVerifier(cfg VerifierConfig) (*Verifier, error) {
	if cfg.Token == nil {
		return nil, errors.New("a credential source is required")
	}
	baseURL := strings.TrimSpace(cfg.BaseURL)
	if baseURL == "" {
		baseURL = defaultBaseURL
	}
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil {
		return nil, errors.New("workspace base URL must be an HTTP(S) endpoint without credentials")
	}
	client := cfg.HTTPClient
	if client == nil {
		timeout := cfg.Timeout
		if timeout <= 0 {
			timeout = 30 * time.Second
		}
		client = &http.Client{Timeout: timeout}
	}
	return &Verifier{baseURL: strings.TrimSuffix(baseURL, "/"), token: cfg.Token, client: client}, nil
}

// DirectoryID returns the user's immutable numeric Directory id.
func (v *Verifier) DirectoryID(ctx context.Context, email string) (string, error) {
	if v == nil {
		return "", errors.New("workspace verifier is not configured")
	}
	if strings.TrimSpace(email) == "" {
		return "", errors.New("email is required")
	}
	token, err := v.token(ctx)
	if err != nil {
		return "", fmt.Errorf("workspace credential: %w", err)
	}
	if strings.TrimSpace(token) == "" {
		return "", errors.New("workspace credential is empty; check the token source configuration")
	}
	endpoint := v.baseURL + "/admin/directory/v1/users/" + url.PathEscape(email) + "?fields=id,primaryEmail"
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", fmt.Errorf("build directory request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := v.client.Do(request)
	if err != nil {
		return "", fmt.Errorf("read directory user: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("read directory user: %w", err)
	}
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("read directory user: status %d", response.StatusCode)
	}
	var user struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(body, &user); err != nil {
		return "", fmt.Errorf("decode directory user: %w", err)
	}
	if !validMemberID(user.ID) {
		return "", fmt.Errorf("directory user %q has a non-numeric id", email)
	}
	return user.ID, nil
}

// VerifySubject fails unless the Directory id for email equals expectedSub.
// ExpectedSub may be either the bare id or a full subject such as
// user:google/sub/<id>; the bare id is compared.
func (v *Verifier) VerifySubject(ctx context.Context, email, expectedSub string) error {
	expected := strings.TrimSpace(expectedSub)
	if index := strings.LastIndex(expected, "/"); index >= 0 {
		expected = expected[index+1:]
	}
	if expected == "" {
		return errors.New("expected subject is required")
	}
	actual, err := v.DirectoryID(ctx, email)
	if err != nil {
		return err
	}
	if actual != expected {
		return fmt.Errorf("gate 1 failed: directory id %s does not match the token subject %s for %s", actual, expected, email)
	}
	return nil
}
