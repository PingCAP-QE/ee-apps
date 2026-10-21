package authn

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeTokenFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReview(t *testing.T) {
	var gotAuth, gotToken string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		var body struct {
			Spec struct {
				Token string `json:"token"`
			} `json:"spec"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		gotToken = body.Spec.Token
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":{"authenticated":true,"user":{"username":"system:serviceaccount:ci:checkout"}}}`)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	caller, err := reviewer.Review(context.Background(), "caller-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if caller.Namespace != "ci" || caller.ServiceAccount != "checkout" {
		t.Fatalf("unexpected caller: %#v", caller)
	}
	if gotAuth != "Bearer sa-token" {
		t.Fatalf("unexpected authorization header: %q", gotAuth)
	}
	if gotToken != "caller-token" {
		t.Fatalf("unexpected reviewed token: %q", gotToken)
	}
}

func TestReviewUnauthenticated(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"status":{"authenticated":false}}`)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reviewer.Review(context.Background(), "bad"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("expected ErrUnauthenticated, got %v", err)
	}
}

func TestReviewNonServiceAccount(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"status":{"authenticated":true,"user":{"username":"alice"}}}`)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reviewer.Review(context.Background(), "token"); err == nil {
		t.Fatal("expected an error")
	}
}

func TestReviewBackendFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reviewer.Review(context.Background(), "token"); err == nil {
		t.Fatal("expected an error")
	}
}

func TestReviewEmptyToken(t *testing.T) {
	reviewer, err := New("http://example.invalid", writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reviewer.Review(context.Background(), "  "); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("expected ErrUnauthenticated, got %v", err)
	}
}

func TestReviewMissingTokenFile(t *testing.T) {
	reviewer, err := New("http://example.invalid", filepath.Join(t.TempDir(), "missing"), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reviewer.Review(context.Background(), "token"); err == nil {
		t.Fatal("expected an error")
	}
}

func TestNewRequiresAPIURL(t *testing.T) {
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	t.Setenv("KUBERNETES_SERVICE_PORT", "")
	if _, err := New("", writeTokenFile(t), "", time.Second); err == nil {
		t.Fatal("expected an error without an API URL")
	}
	t.Setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
	t.Setenv("KUBERNETES_SERVICE_PORT", "443")
	if _, err := New("", writeTokenFile(t), "", time.Second); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNewRejectsInvalidCA(t *testing.T) {
	ca := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(ca, []byte("not a cert"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := New("https://example.invalid", writeTokenFile(t), ca, time.Second); err == nil {
		t.Fatal("expected an error for an invalid CA file")
	}
}

func TestReady(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/version" {
			http.NotFound(w, r)
			return
		}
		if got := r.Header.Get("Authorization"); got != "Bearer sa-token" {
			t.Errorf("unexpected authorization: %q", got)
		}
		_, _ = io.WriteString(w, `{"major":"1","minor":"34"}`)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := reviewer.Ready(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestReadyFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	reviewer, err := New(server.URL, writeTokenFile(t), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := reviewer.Ready(context.Background()); err == nil {
		t.Fatal("expected an error")
	}
}

func TestReadyMissingTokenFile(t *testing.T) {
	reviewer, err := New("http://example.invalid", filepath.Join(t.TempDir(), "missing"), "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := reviewer.Ready(context.Background()); err == nil {
		t.Fatal("expected an error")
	}
}
