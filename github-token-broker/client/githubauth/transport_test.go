package githubauth

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
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
	if len(lastPerm) != 1 || lastPerm["contents"] != "read" {
		t.Fatalf("broker permissions = %#v, want contents=read only", lastPerm)
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
	if len(lastPerm) != 1 || lastPerm["contents"] != "write" {
		t.Fatalf("broker permissions = %#v, want contents=write only", lastPerm)
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

func TestTransportPinnedRepoMintsForNonRepositoryRequests(t *testing.T) {
	var (
		mu       sync.Mutex
		calls    int
		lastRepo string
		lastPerm map[string]string
	)
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		calls++
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
			"token":      "pinned-token",
			"expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
		})
	}))
	defer broker.Close()

	var auths []string
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		auths = append(auths, req.Header.Get("Authorization"))
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	tr := NewTransport(Config{
		BrokerURL:   broker.URL,
		TokenFile:   tokenFile,
		Repo:        "pingcap/tidb",
		Permissions: map[string]string{"members": "read", "administration": "read"},
		Base:        base,
	})
	client := &http.Client{Transport: tr}

	for _, path := range []string{
		"https://api.github.com/orgs/pingcap/members?role=admin",
		"https://api.github.com/users/pingcap",
		"https://api.github.com/repos/pingcap/tidb/collaborators",
	} {
		resp, err := client.Get(path)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
	}

	mu.Lock()
	defer mu.Unlock()
	if calls != 1 {
		t.Fatalf("broker calls = %d, want 1 (same repo and permissions are cached)", calls)
	}
	if lastRepo != "pingcap/tidb" {
		t.Fatalf("broker repo = %q, want pinned repo", lastRepo)
	}
	if !reflect.DeepEqual(lastPerm, map[string]string{"members": "read", "administration": "read"}) {
		t.Fatalf("broker permissions = %#v, want the pinned permission set", lastPerm)
	}
	for _, auth := range auths {
		if auth != "Bearer pinned-token" {
			t.Fatalf("Authorization = %q, want the pinned broker token", auth)
		}
	}
}

func TestTransportPinnedRepoDoesNotLeakToOtherHosts(t *testing.T) {
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
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: NewTransport(Config{
		BrokerURL:   broker.URL,
		TokenFile:   tokenFile,
		Repo:        "pingcap/tidb",
		Permissions: map[string]string{"members": "read"},
		Base:        base,
	})}

	resp, err := client.Get("https://objects.githubusercontent.com/artifact?sig=x")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if calls != 0 {
		t.Fatalf("broker calls = %d, want 0 for non-GitHub API hosts", calls)
	}
	if auth != "" {
		t.Fatalf("Authorization = %q, want none for signed download URLs", auth)
	}
}

func TestTransportPinnedRepoReportsFallback(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer broker.Close()

	var auth string
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		auth = req.Header.Get("Authorization")
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	type fallbackEvent struct {
		repo string
		err  error
	}
	var events []fallbackEvent
	client := &http.Client{Transport: NewTransport(Config{
		BrokerURL:     broker.URL,
		TokenFile:     tokenFile,
		FallbackToken: "pat-token",
		Repo:          "pingcap/tidb",
		Permissions:   map[string]string{"members": "read"},
		Base:          base,
		OnFallback: func(repo string, err error) {
			events = append(events, fallbackEvent{repo: repo, err: err})
		},
	})}

	resp, err := client.Get("https://api.github.com/orgs/pingcap/members")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if auth != "Bearer pat-token" {
		t.Fatalf("Authorization = %q, want the fallback token", auth)
	}
	if len(events) != 1 {
		t.Fatalf("fallback events = %d, want 1", len(events))
	}
	if events[0].repo != "pingcap/tidb" || events[0].err == nil {
		t.Fatalf("fallback event = %#v, want repo and error", events[0])
	}
}

func TestTransportPinnedRepoReturnsErrorWithoutFallback(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer broker.Close()

	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		t.Fatal("GitHub request must not be sent without a token")
		return nil, nil
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: NewTransport(Config{
		BrokerURL:   broker.URL,
		TokenFile:   tokenFile,
		Repo:        "pingcap/tidb",
		Permissions: map[string]string{"members": "read"},
		Base:        base,
	})}
	if _, err := client.Get("https://api.github.com/orgs/pingcap/members"); err == nil {
		t.Fatal("expected broker error")
	}
}

func TestTransportSeparatesCacheEntriesByPermissionSet(t *testing.T) {
	var (
		mu        sync.Mutex
		permCalls []map[string]string
	)
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		var body struct {
			Permissions map[string]string `json:"permissions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		permCalls = append(permCalls, body.Permissions)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"token":"tok","expires_at":"` + time.Now().Add(time.Hour).UTC().Format(time.RFC3339) + `"}`))
	}))
	defer broker.Close()

	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(http.NoBody), Request: req}, nil
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: NewTransport(Config{
		BrokerURL: broker.URL,
		TokenFile: tokenFile,
		Base:      base,
	})}

	// Two permission sets for the same repository must not share a cache entry.
	for i := 0; i < 2; i++ {
		for _, path := range []string{
			"https://api.github.com/repos/o/r/actions/runs",
			"https://api.github.com/repos/o/r/issues/1",
		} {
			resp, err := client.Get(path)
			if err != nil {
				t.Fatal(err)
			}
			_ = resp.Body.Close()
		}
	}

	mu.Lock()
	defer mu.Unlock()
	if len(permCalls) != 2 {
		t.Fatalf("broker calls = %d, want 2 (one per permission set)", len(permCalls))
	}
}

func TestPermissionsForRequest(t *testing.T) {
	tests := []struct {
		name   string
		method string
		path   string
		want   map[string]string
	}{
		{"git ref read", http.MethodGet, "/repos/o/r/git/ref/heads/main", map[string]string{"contents": "read"}},
		{"branch read", http.MethodGet, "/repos/o/r/branches/main", map[string]string{"contents": "read"}},
		{"tag list read", http.MethodGet, "/repos/o/r/tags", map[string]string{"contents": "read"}},
		{"compare read", http.MethodGet, "/repos/o/r/compare/a...b", map[string]string{"contents": "read"}},
		{"pr read", http.MethodGet, "/repos/o/r/pulls/42", map[string]string{"pull_requests": "read"}},
		{"commit prs read", http.MethodGet, "/repos/o/r/commits/abc/pulls", map[string]string{"pull_requests": "read"}},
		{"tag create", http.MethodPost, "/repos/o/r/git/tags", map[string]string{"contents": "write"}},
		{"ref create", http.MethodPost, "/repos/o/r/git/refs", map[string]string{"contents": "write"}},
		{"pr create", http.MethodPost, "/repos/o/r/pulls", map[string]string{"pull_requests": "write"}},
		{"issue read", http.MethodGet, "/repos/o/r/issues/1", map[string]string{"issues": "read"}},
		{"issue write", http.MethodPost, "/repos/o/r/issues", map[string]string{"issues": "write"}},
		{"action read", http.MethodGet, "/repos/o/r/actions/runs", map[string]string{"actions": "read"}},
		{"repo root read", http.MethodGet, "/repos/o/r", map[string]string{"contents": "read"}},
		{"similar name is contents", http.MethodGet, "/repos/o/r/issues-foo", map[string]string{"contents": "read"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := permissionsForRequest(tt.method, tt.path)
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("permissionsForRequest(%q, %q) = %#v, want %#v", tt.method, tt.path, got, tt.want)
			}
		})
	}
}
