package gitcred

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testTokenFileContent = "caller-sa-token\n"

func newTestHelper(t *testing.T, brokerURL string, mutate func(*Config)) *Helper {
	t.Helper()
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "sa.token")
	if err := os.WriteFile(tokenFile, []byte(testTokenFileContent), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}
	cfg := Config{
		BrokerURL:   brokerURL,
		TokenFile:   tokenFile,
		CacheDir:    filepath.Join(dir, "cache"),
		Permissions: map[string]string{"contents": "read"},
		Timeout:     2 * time.Second,
		Margin:      5 * time.Minute,
		Now:         func() time.Time { return time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC) },
	}
	if mutate != nil {
		mutate(&cfg)
	}
	helper, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return helper
}

func TestGetMintsTokenFromBroker(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/v1/token" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer "+strings.TrimSpace(testTokenFileContent) {
			t.Errorf("unexpected authorization header %q", got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("unexpected content type %q", got)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		if payload["repo"] != "tidbcloud/cloud-storage-engine" {
			t.Errorf("unexpected repo %v", payload["repo"])
		}
		perms, ok := payload["permissions"].(map[string]any)
		if !ok || perms["contents"] != "read" {
			t.Errorf("unexpected permissions %v", payload["permissions"])
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2026-10-27T12:55:00Z","installation_id":42,"source":"app"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	got, err := helper.Get(context.Background(), Request{
		Protocol: "https",
		Host:     "github.com",
		Path:     "tidbcloud/cloud-storage-engine.git",
	})
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Username != defaultUsername {
		t.Errorf("username = %q, want %q", got.Username, defaultUsername)
	}
	if got.Password != "ghs_test" {
		t.Errorf("password = %q, want the minted token", got.Password)
	}
	if requests.Load() != 1 {
		t.Errorf("broker calls = %d, want 1", requests.Load())
	}
	if mode := helper.AuthMode(); mode != ModeApp {
		t.Errorf("auth mode = %q, want %q", mode, ModeApp)
	}
}

func TestGetCachesTokenPerRepository(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		_, _ = io.WriteString(w, `{"token":"ghs_cached","expires_at":"2026-10-27T12:55:00Z","installation_id":42,"source":"app"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	paths := []string{
		"tidbcloud/cloud-storage-engine.git",
		"tidbcloud/docker-image-controller.git",
		"tidbcloud/another-repo",
	}
	for _, path := range paths {
		got, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: path})
		if err != nil {
			t.Fatalf("Get(%s): %v", path, err)
		}
		if got.Password != "ghs_cached" {
			t.Errorf("Get(%s) password = %q", path, got.Password)
		}
	}
	// Every repository must be authorised by the broker: a cached
	// installation token would otherwise bypass the repository allowlist.
	if requests.Load() != int32(len(paths)) {
		t.Errorf("broker calls = %d, want %d (one per repository)", requests.Load(), len(paths))
	}

	if _, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "tidbcloud/another-repo"}); err != nil {
		t.Fatalf("Get(cached repo): %v", err)
	}
	if requests.Load() != int32(len(paths)) {
		t.Errorf("broker calls = %d, want %d (repeat repository served from cache)", requests.Load(), len(paths))
	}
}

func TestGetRemintsAfterExpiry(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		// Expires 10 minutes after the helper clock, but the 5 minute safety
		// margin makes the first token stale after the clock moves.
		expiry := time.Date(2026, 10, 27, 12, 10, 0, 0, time.UTC)
		_, _ = io.WriteString(w, `{"token":"ghs_`+time.Now().Format("150405.000000000")+`","expires_at":"`+expiry.Format(time.RFC3339)+`"}`)
	}))
	defer server.Close()

	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	helper := newTestHelper(t, server.URL, func(cfg *Config) {
		cfg.Now = func() time.Time { return now }
	})
	req := Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"}
	if _, err := helper.Get(context.Background(), req); err != nil {
		t.Fatalf("first Get: %v", err)
	}
	now = now.Add(6 * time.Minute)
	if _, err := helper.Get(context.Background(), req); err != nil {
		t.Fatalf("second Get: %v", err)
	}
	if requests.Load() != 2 {
		t.Errorf("broker calls = %d, want 2 (refresh inside the safety margin)", requests.Load())
	}
}

func TestGetSkipsNonGitHubHosts(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	for _, req := range []Request{
		{Protocol: "https", Host: "objects.githubusercontent.com", Path: "tidbcloud/cloud-storage-engine.git"},
		{Protocol: "https", Host: "gitlab.com", Path: "tidbcloud/cloud-storage-engine.git"},
		{Protocol: "ssh", Host: "github.com", Path: "tidbcloud/cloud-storage-engine.git"},
	} {
		got, err := helper.Get(context.Background(), req)
		if err != nil {
			t.Fatalf("Get(%s): %v", req.Host, err)
		}
		if got != (Response{}) {
			t.Errorf("Get(%s) = %+v, want empty credentials", req.Host, got)
		}
	}
	if requests.Load() != 0 {
		t.Errorf("broker calls = %d, want 0", requests.Load())
	}
}

func TestGetRequiresRepositoryPath(t *testing.T) {
	helper := newTestHelper(t, "http://127.0.0.1:1", nil)
	got, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com"})
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got != (Response{}) {
		t.Errorf("Get = %+v, want empty credentials", got)
	}
}

func TestGetReportsBrokerFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"message":"permission is not allowed: contents"}`, http.StatusForbidden)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	_, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err == nil {
		t.Fatal("Get succeeded, want a broker error")
	}
	if strings.Contains(err.Error(), "ghs_") {
		t.Errorf("error leaks token material: %v", err)
	}
	if !strings.Contains(err.Error(), "403") {
		t.Errorf("error = %v, want the broker status code", err)
	}
	if mode := helper.AuthMode(); mode != "" {
		t.Errorf("auth mode = %q, want empty after a failed mint", mode)
	}
}

func TestGetFallsBackToCredentialsFile(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "no installation", http.StatusForbidden)
	}))
	defer server.Close()

	dir := t.TempDir()
	fallback := filepath.Join(dir, ".git-credentials")
	if err := os.WriteFile(fallback, []byte("https://pat-user:pat-secret@github.com\n"), 0o600); err != nil {
		t.Fatalf("write fallback: %v", err)
	}
	helper := newTestHelper(t, server.URL, func(cfg *Config) { cfg.FallbackCredentialsFile = fallback })

	got, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Username != "pat-user" || got.Password != "pat-secret" {
		t.Errorf("credentials = %+v, want the PAT fallback", got)
	}
	if mode := helper.AuthMode(); mode != ModePAT {
		t.Errorf("auth mode = %q, want %q", mode, ModePAT)
	}
}

func TestGetIgnoresFallbackForOtherHosts(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "no installation", http.StatusForbidden)
	}))
	defer server.Close()

	dir := t.TempDir()
	fallback := filepath.Join(dir, ".git-credentials")
	if err := os.WriteFile(fallback, []byte("https://pat-user:pat-secret@gitlab.com\n"), 0o600); err != nil {
		t.Fatalf("write fallback: %v", err)
	}
	helper := newTestHelper(t, server.URL, func(cfg *Config) { cfg.FallbackCredentialsFile = fallback })

	got, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err == nil {
		t.Fatal("Get succeeded, want the broker error: the fallback hosts another git server")
	}
	if got != (Response{}) {
		t.Errorf("credentials = %+v, want none", got)
	}
	if strings.Contains(err.Error(), "pat-secret") {
		t.Errorf("error leaks the fallback credential: %v", err)
	}
}

func TestEraseRemovesCachedToken(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	req := Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"}
	if _, err := helper.Get(context.Background(), req); err != nil {
		t.Fatalf("first Get: %v", err)
	}
	if err := helper.Erase(req); err != nil {
		t.Fatalf("Erase: %v", err)
	}
	if _, err := helper.Get(context.Background(), req); err != nil {
		t.Fatalf("second Get: %v", err)
	}
	if requests.Load() != 2 {
		t.Errorf("broker calls = %d, want 2", requests.Load())
	}
}

func TestGetRetriesTransientFailures(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 1 {
			http.Error(w, "boom", http.StatusBadGateway)
			return
		}
		_, _ = io.WriteString(w, `{"token":"ghs_retry","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	got, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Password != "ghs_retry" {
		t.Errorf("password = %q, want the retried token", got.Password)
	}
	if requests.Load() != 2 {
		t.Errorf("broker calls = %d, want 2", requests.Load())
	}
}

func TestParseRequest(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want Request
	}{
		{
			name: "full request",
			in:   "protocol=https\nhost=github.com\npath=tidbcloud/cloud-storage-engine.git\n\n",
			want: Request{Protocol: "https", Host: "github.com", Path: "tidbcloud/cloud-storage-engine.git"},
		},
		{
			name: "crlf line endings",
			in:   "protocol=https\r\nhost=github.com\r\npath=pingcap/tidb.git\r\n\r\n",
			want: Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"},
		},
		{
			name: "leading slash path",
			in:   "protocol=https\nhost=github.com\npath=/tikv/tikv.git\n\n",
			want: Request{Protocol: "https", Host: "github.com", Path: "tikv/tikv.git"},
		},
		{
			name: "no trailing blank line",
			in:   "protocol=https\nhost=github.com\npath=pingcap/tidb\n",
			want: Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb"},
		},
		{
			name: "unknown keys are ignored",
			in:   "protocol=https\nhost=github.com\npath=pingcap/tidb\nusername=x-access-token\npassword=secret\n\n",
			want: Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseRequest(strings.NewReader(tc.in))
			if err != nil {
				t.Fatalf("ParseRequest: %v", err)
			}
			if got != tc.want {
				t.Errorf("ParseRequest = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestResponseWrite(t *testing.T) {
	var b strings.Builder
	if err := (Response{Username: "x-access-token", Password: "ghs_token"}).Write(&b); err != nil {
		t.Fatalf("Write: %v", err)
	}
	want := "username=x-access-token\npassword=ghs_token\n\n"
	if b.String() != want {
		t.Errorf("Write = %q, want %q", b.String(), want)
	}
}

func TestRepoFromPath(t *testing.T) {
	tests := []struct {
		path      string
		wantOwner string
		wantRepo  string
		wantErr   bool
	}{
		{path: "tidbcloud/cloud-storage-engine.git", wantOwner: "tidbcloud", wantRepo: "tidbcloud/cloud-storage-engine"},
		{path: "pingcap/tidb", wantOwner: "pingcap", wantRepo: "pingcap/tidb"},
		{path: "/tikv/pd.git/", wantOwner: "tikv", wantRepo: "tikv/pd"},
		{path: "pingcap/tidb/sub/dir", wantOwner: "pingcap", wantRepo: "pingcap/tidb"},
		{path: "pingcap", wantErr: true},
		{path: "", wantErr: true},
	}
	for _, tc := range tests {
		owner, repo, err := repoFromPath(tc.path)
		if tc.wantErr {
			if err == nil {
				t.Errorf("repoFromPath(%q) succeeded, want error", tc.path)
			}
			continue
		}
		if err != nil {
			t.Errorf("repoFromPath(%q): %v", tc.path, err)
			continue
		}
		if owner != tc.wantOwner || repo != tc.wantRepo {
			t.Errorf("repoFromPath(%q) = (%q, %q), want (%q, %q)", tc.path, owner, repo, tc.wantOwner, tc.wantRepo)
		}
	}
}

func TestNewValidatesConfig(t *testing.T) {
	if _, err := New(Config{}); err == nil {
		t.Error("New with empty config succeeded, want error")
	}
}

func TestNewCacheDirIsPrivate(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "sa.token")
	if err := os.WriteFile(tokenFile, []byte(testTokenFileContent), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}
	cacheDir := filepath.Join(dir, "cache")
	helper, err := New(Config{BrokerURL: "http://127.0.0.1:1", TokenFile: tokenFile, CacheDir: cacheDir})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if helper == nil {
		t.Fatal("New returned nil helper")
	}
	info, err := os.Stat(cacheDir)
	if err != nil {
		t.Fatalf("stat cache dir: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Errorf("cache dir mode = %o, want 700", perm)
	}
}

func TestGetWritesStateFile(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"token":"ghs_test","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "git-auth-state")
	helper := newTestHelper(t, server.URL, func(cfg *Config) { cfg.StateFile = statePath })
	if _, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"}); err != nil {
		t.Fatalf("Get: %v", err)
	}
	content, err := os.ReadFile(statePath)
	if err != nil {
		t.Fatalf("read state file: %v", err)
	}
	if got := strings.TrimSpace(string(content)); got != ModeApp {
		t.Errorf("state file = %q, want %q", got, ModeApp)
	}
}

func TestMissingCallerTokenFile(t *testing.T) {
	helper := newTestHelper(t, "http://127.0.0.1:1", nil)
	if err := os.Remove(helper.cfg.TokenFile); err != nil {
		t.Fatalf("remove token file: %v", err)
	}
	_, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err == nil {
		t.Fatal("Get succeeded without a caller token, want error")
	}
	if !strings.Contains(err.Error(), "caller") {
		t.Errorf("error = %v, want it to mention the caller token", err)
	}
}

func TestGetRejectsMalformedBrokerResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"token":"","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	helper := newTestHelper(t, server.URL, nil)
	_, err := helper.Get(context.Background(), Request{Protocol: "https", Host: "github.com", Path: "pingcap/tidb.git"})
	if err == nil {
		t.Fatal("Get accepted an empty token, want error")
	}
	if !errors.Is(err, errEmptyToken) {
		t.Errorf("error = %v, want errEmptyToken", err)
	}
}
