package githubauth

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func testTransport(t *testing.T, brokerURL, fallback string, base http.RoundTripper) *Transport {
	t.Helper()
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return NewTransport(Config{
		BrokerURL:     brokerURL,
		TokenFile:     tokenFile,
		FallbackToken: fallback,
		Base:          base,
	})
}

func TestTransportMintsScopedTokenAndCaches(t *testing.T) {
	var (
		mu       sync.Mutex
		calls    int
		lastRepo string
		lastPerm map[string]string
		lastAuth string
	)
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		calls++
		lastAuth = r.Header.Get("Authorization")
		var body struct {
			Repo        string            `json:"repo"`
			Permissions map[string]string `json:"permissions"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode broker request: %v", err)
		}
		lastRepo, lastPerm = body.Repo, body.Permissions
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"token":      "broker-token",
			"expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
		})
	}))
	defer broker.Close()

	var githubAuth string
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		githubAuth = req.Header.Get("Authorization")
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(http.NoBody),
			Request:    req,
		}, nil
	})
	tr := testTransport(t, broker.URL, "", base)
	client := &http.Client{Transport: tr}

	for i := 0; i < 2; i++ {
		resp, err := client.Get("https://api.github.com/repos/tidbcloud/cloud-storage-engine/git/ref/heads/main")
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
	}

	if githubAuth != "Bearer broker-token" {
		t.Fatalf("github Authorization = %q", githubAuth)
	}
	mu.Lock()
	defer mu.Unlock()
	if calls != 1 {
		t.Fatalf("broker calls = %d, want 1 (cached)", calls)
	}
	if lastAuth != "Bearer sa-token" {
		t.Fatalf("broker caller Authorization = %q", lastAuth)
	}
	if lastRepo != "tidbcloud/cloud-storage-engine" {
		t.Fatalf("broker repo = %q", lastRepo)
	}
	if lastPerm["contents"] != "read" || lastPerm["pull_requests"] != "read" {
		t.Fatalf("broker permissions = %#v", lastPerm)
	}
}

func TestTransportUsesWritePermissionsForMutations(t *testing.T) {
	var lastPerm map[string]string
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Permissions map[string]string `json:"permissions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		lastPerm = body.Permissions
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"token":"broker-token","expires_at":"` + time.Now().Add(time.Hour).UTC().Format(time.RFC3339) + `"}`))
	}))
	defer broker.Close()

	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	client := &http.Client{Transport: testTransport(t, broker.URL, "", base)}

	req, err := http.NewRequest(http.MethodPost, "https://api.github.com/repos/tidbcloud/cloud-storage-engine/git/refs", http.NoBody)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if lastPerm["contents"] != "write" || lastPerm["pull_requests"] != "write" {
		t.Fatalf("broker permissions = %#v", lastPerm)
	}
}

func TestTransportFallsBackWhenBrokerDenies(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer broker.Close()

	var auth string
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		auth = req.Header.Get("Authorization")
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	client := &http.Client{Transport: testTransport(t, broker.URL, "pat-token", base)}

	resp, err := client.Get("https://api.github.com/repos/pingcap/tidb/git/ref/heads/master")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if auth != "Bearer pat-token" {
		t.Fatalf("Authorization = %q, want static fallback", auth)
	}
}

func TestTransportSkipsBrokerForNonRepositoryRequests(t *testing.T) {
	var calls int
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	defer broker.Close()

	var auth string
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		auth = req.Header.Get("Authorization")
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	client := &http.Client{Transport: testTransport(t, broker.URL, "pat-token", base)}

	resp, err := client.Get("https://api.github.com/rate_limit")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if calls != 0 {
		t.Fatalf("broker calls = %d, want 0", calls)
	}
	if auth != "Bearer pat-token" {
		t.Fatalf("Authorization = %q, want static fallback", auth)
	}
}

func TestTransportReturnsErrorWithoutFallback(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer broker.Close()

	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		t.Fatal("GitHub request must not be sent")
		return nil, nil
	})
	client := &http.Client{Transport: testTransport(t, broker.URL, "", base)}
	if _, err := client.Get("https://api.github.com/repos/pingcap/tidb"); err == nil {
		t.Fatal("expected broker error")
	}
}
