package dl

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	c, err := New(server.URL, "issuer-secret", server.Client())
	if err != nil {
		t.Fatal(err)
	}
	c.retries = 0
	c.sleep = func(time.Duration) {}
	return c
}

func TestIssueSuccess(t *testing.T) {
	c := newClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/shares" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer issuer-secret" {
			t.Errorf("authorization = %q", got)
		}
		var payload struct {
			Artifact struct {
				Repository string `json:"repository"`
				Tag        string `json:"tag"`
				File       string `json:"file"`
			} `json:"artifact"`
			Expires int64  `json:"expires_in_seconds"`
			Purpose string `json:"purpose"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode: %v", err)
		}
		if payload.Artifact.Repository != "r.example.com/a" || payload.Artifact.Tag != "v1" || payload.Artifact.File != "a.tar.gz" || payload.Expires != 60 {
			t.Errorf("payload = %+v", payload)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"share-1","download_url":"https://dl.example.com/d/token"}`))
	})
	share, err := c.Issue(context.Background(), IssueRequest{
		Repository: "r.example.com/a", Tag: "v1", File: "a.tar.gz", ExpiresInSeconds: 60, Purpose: "customer-delivery",
	})
	if err != nil {
		t.Fatal(err)
	}
	if share.ID != "share-1" || share.DownloadURL != "https://dl.example.com/d/token" {
		t.Fatalf("share = %+v", share)
	}
}

func TestIssueErrors(t *testing.T) {
	tests := []struct {
		status int
		want   error
	}{
		{http.StatusBadRequest, ErrInvalidRequest},
		{http.StatusUnauthorized, ErrUnauthorized},
		{http.StatusForbidden, ErrUnauthorized},
		{http.StatusNotFound, ErrNotFound},
	}
	for _, tt := range tests {
		c := newClient(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(tt.status) })
		_, err := c.Issue(context.Background(), IssueRequest{Repository: "r", Tag: "t", File: "f", Purpose: "p"})
		if !errors.Is(err, tt.want) {
			t.Fatalf("status %d error = %v, want %v", tt.status, err, tt.want)
		}
	}
}

func TestIssueRetriesServerErrors(t *testing.T) {
	calls := 0
	c := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	c.retries = 2
	_, err := c.Issue(context.Background(), IssueRequest{Repository: "r", Tag: "t", File: "f", Purpose: "p"})
	if !errors.Is(err, ErrUnavailable) {
		t.Fatalf("error = %v", err)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3", calls)
	}
}

func TestIssueMalformedResponse(t *testing.T) {
	for _, body := range []string{`not-json`, `{"id":"x"}`, `{}`} {
		c := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(body))
		})
		if _, err := c.Issue(context.Background(), IssueRequest{Repository: "r", Tag: "t", File: "f", Purpose: "p"}); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("body %q error = %v", body, err)
		}
	}
}

func TestRevoke(t *testing.T) {
	for _, status := range []int{http.StatusNoContent, http.StatusNotFound} {
		c := newClient(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete || r.URL.Path != "/api/v1/shares/share-1" {
				t.Errorf("request = %s %s", r.Method, r.URL.Path)
			}
			w.WriteHeader(status)
		})
		if err := c.Revoke(context.Background(), "share-1"); err != nil {
			t.Fatalf("status %d error = %v", status, err)
		}
	}
}

func TestNewValidation(t *testing.T) {
	for _, base := range []string{"", "not-a-url", "ftp://example.com", "https://user@example.com"} {
		if _, err := New(base, "secret", nil); err == nil {
			t.Fatalf("base %q accepted", base)
		}
	}
	if _, err := New("https://example.com", "", nil); err == nil {
		t.Fatal("empty secret accepted")
	}
}
