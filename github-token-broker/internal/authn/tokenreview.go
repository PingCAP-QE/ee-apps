package authn

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
)

// ErrUnauthenticated is returned when TokenReview does not authenticate the caller.
var ErrUnauthenticated = errors.New("caller is not authenticated")

// Reviewer authenticates a caller from its bearer token.
type Reviewer interface {
	Review(ctx context.Context, token string) (authz.Caller, error)
}

// TokenReviewer authenticates callers with the Kubernetes TokenReview API,
// using the broker pod's own ServiceAccount token.
type TokenReviewer struct {
	apiURL    string
	tokenFile string
	client    *http.Client
}

// New creates a TokenReviewer. apiURL defaults to the in-cluster API server.
func New(apiURL, tokenFile, caFile string, timeout time.Duration) (*TokenReviewer, error) {
	if apiURL == "" {
		apiURL = inClusterAPIURL()
	}
	if apiURL == "" {
		return nil, errors.New("kubernetes api url is not configured and in-cluster discovery failed")
	}
	if tokenFile == "" {
		tokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	}
	if timeout <= 0 {
		timeout = 10 * time.Second
	}

	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("read kubernetes ca: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(data) {
			return nil, errors.New("kubernetes ca file contains no certificates")
		}
		tlsConfig.RootCAs = pool
	}

	return &TokenReviewer{
		apiURL:    strings.TrimRight(apiURL, "/"),
		tokenFile: tokenFile,
		client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				TLSClientConfig: tlsConfig,
			},
		},
	}, nil
}

// Ready checks that the Kubernetes API is reachable with the broker's
// ServiceAccount token.
func (r *TokenReviewer) Ready(ctx context.Context) error {
	saToken, err := os.ReadFile(r.tokenFile)
	if err != nil {
		return fmt.Errorf("read service account token: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, r.apiURL+"/version", nil)
	if err != nil {
		return fmt.Errorf("create kubernetes request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(saToken)))
	resp, err := r.client.Do(req)
	if err != nil {
		return fmt.Errorf("kubernetes api request: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("kubernetes api returned status %d", resp.StatusCode)
	}
	return nil
}

// Review validates token and returns the calling ServiceAccount.
func (r *TokenReviewer) Review(ctx context.Context, token string) (authz.Caller, error) {
	if strings.TrimSpace(token) == "" {
		return authz.Caller{}, ErrUnauthenticated
	}
	saToken, err := os.ReadFile(r.tokenFile)
	if err != nil {
		return authz.Caller{}, fmt.Errorf("read service account token: %w", err)
	}

	body := map[string]any{
		"apiVersion": "authentication.k8s.io/v1",
		"kind":       "TokenReview",
		"spec": map[string]string{
			"token": token,
		},
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return authz.Caller{}, fmt.Errorf("marshal token review: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.apiURL+"/apis/authentication.k8s.io/v1/tokenreviews", bytes.NewReader(payload))
	if err != nil {
		return authz.Caller{}, fmt.Errorf("create token review: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(saToken)))
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.client.Do(req)
	if err != nil {
		return authz.Caller{}, fmt.Errorf("token review request: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return authz.Caller{}, fmt.Errorf("token review failed: status %d: %s", resp.StatusCode, strings.TrimSpace(string(snippet)))
	}

	var result struct {
		Status struct {
			Authenticated bool   `json:"authenticated"`
			Error         string `json:"error"`
			User          struct {
				Username string `json:"username"`
			} `json:"user"`
		} `json:"status"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&result); err != nil {
		return authz.Caller{}, fmt.Errorf("decode token review: %w", err)
	}
	if !result.Status.Authenticated {
		return authz.Caller{}, ErrUnauthenticated
	}
	namespace, serviceAccount, ok := parseServiceAccount(result.Status.User.Username)
	if !ok {
		return authz.Caller{}, fmt.Errorf("token review returned a non-service-account identity: %s", result.Status.User.Username)
	}
	return authz.Caller{Namespace: namespace, ServiceAccount: serviceAccount}, nil
}

func parseServiceAccount(username string) (string, string, bool) {
	parts := strings.Split(username, ":")
	if len(parts) != 4 || parts[0] != "system" || parts[1] != "serviceaccount" || parts[2] == "" || parts[3] == "" {
		return "", "", false
	}
	return parts[2], parts[3], true
}

func inClusterAPIURL() string {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return ""
	}
	return "https://" + net.JoinHostPort(host, port)
}
