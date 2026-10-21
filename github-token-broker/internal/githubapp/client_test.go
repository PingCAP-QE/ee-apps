package githubapp

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeTestKey(t *testing.T, pkcs8 bool) string {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	var block *pem.Block
	if pkcs8 {
		der, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			t.Fatal(err)
		}
		block = &pem.Block{Type: "PRIVATE KEY", Bytes: der}
	} else {
		block = &pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}
	}
	path := filepath.Join(t.TempDir(), "key.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestMint(t *testing.T) {
	var tokenRequest struct {
		Repositories []string          `json:"repositories"`
		Permissions  map[string]string `json:"permissions"`
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, "Bearer ") {
			t.Errorf("missing bearer token: %q", auth)
		}
		if parts := strings.Split(strings.TrimPrefix(auth, "Bearer "), "."); len(parts) != 3 {
			t.Errorf("expected a JWT, got %q", auth)
		}
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v3/repos/acme/widget/installation":
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"id":42}`)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v3/app/installations/42/access_tokens":
			if err := json.NewDecoder(r.Body).Decode(&tokenRequest); err != nil {
				t.Errorf("decode request: %v", err)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2030-01-02T03:04:05Z"}`)
		default:
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := New(123456, writeTestKey(t, false), server.URL, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	token, err := client.Mint(context.Background(), "acme", "widget", []string{"widget"}, map[string]string{"contents": "read"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token.Value != "ghs_test" || token.InstallationID != 42 {
		t.Fatalf("unexpected token: %#v", token)
	}
	if want := time.Date(2030, 1, 2, 3, 4, 5, 0, time.UTC); !token.ExpiresAt.Equal(want) {
		t.Fatalf("unexpected expiry: %v", token.ExpiresAt)
	}
	if len(tokenRequest.Repositories) != 1 || tokenRequest.Repositories[0] != "widget" {
		t.Fatalf("unexpected repositories: %#v", tokenRequest.Repositories)
	}
	if tokenRequest.Permissions["contents"] != "read" {
		t.Fatalf("unexpected permissions: %#v", tokenRequest.Permissions)
	}
}

func TestMintWithoutNarrowing(t *testing.T) {
	var tokenRequest map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v3/repos/acme/widget/installation":
			_, _ = io.WriteString(w, `{"id":7}`)
		case "/api/v3/app/installations/7/access_tokens":
			_ = json.NewDecoder(r.Body).Decode(&tokenRequest)
			_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2030-01-02T03:04:05Z"}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := New(1, writeTestKey(t, true), server.URL, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Mint(context.Background(), "acme", "widget", nil, nil); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := tokenRequest["repositories"]; ok {
		t.Fatalf("did not expect repositories in request: %#v", tokenRequest)
	}
	if _, ok := tokenRequest["permissions"]; ok {
		t.Fatalf("did not expect permissions in request: %#v", tokenRequest)
	}
}

func TestMintNoInstallation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.NotFound(w, nil)
	}))
	defer server.Close()

	client, err := New(1, writeTestKey(t, false), server.URL, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Mint(context.Background(), "acme", "widget", nil, nil)
	if !errors.Is(err, ErrNoInstallation) {
		t.Fatalf("expected ErrNoInstallation, got %v", err)
	}
}

func TestMintUpstreamFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	client, err := New(1, writeTestKey(t, false), server.URL, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Mint(context.Background(), "acme", "widget", nil, nil)
	if err == nil || errors.Is(err, ErrNoInstallation) {
		t.Fatalf("expected an upstream error, got %v", err)
	}
}

func TestNewRejectsInvalidKeys(t *testing.T) {
	dir := t.TempDir()
	invalid := filepath.Join(dir, "invalid.pem")
	if err := os.WriteFile(invalid, []byte("not a pem"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := New(1, invalid, "", time.Second); err == nil {
		t.Fatal("expected an error for invalid PEM")
	}
	if _, err := New(1, filepath.Join(dir, "missing.pem"), "", time.Second); err == nil {
		t.Fatal("expected an error for a missing key")
	}
}

func TestUnsupportedPermission(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v3/repos/acme/widget/installation" {
			_, _ = io.WriteString(w, `{"id":7}`)
			return
		}
		t.Errorf("unexpected request: %s", r.URL.Path)
		http.NotFound(w, r)
	}))
	defer server.Close()

	client, err := New(1, writeTestKey(t, false), server.URL, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Mint(context.Background(), "acme", "widget", nil, map[string]string{"not_a_permission": "read"})
	if err == nil {
		t.Fatal("expected an error for an unsupported permission")
	}
}
