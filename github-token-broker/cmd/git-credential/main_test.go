package main

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func prepareEnv(t *testing.T, brokerURL string) {
	t.Helper()
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "sa.token")
	if err := os.WriteFile(tokenFile, []byte("caller-token\n"), 0o600); err != nil {
		t.Fatalf("write caller token: %v", err)
	}
	t.Setenv("GIT_AUTH_BROKER_URL", brokerURL)
	t.Setenv("GIT_AUTH_BROKER_TOKEN_FILE", tokenFile)
	t.Setenv("GIT_AUTH_CACHE_DIR", filepath.Join(dir, "cache"))
}

func TestRunGetWritesCredentials(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"token":"ghs_cli","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()
	prepareEnv(t, server.URL)

	var stdout, stderr bytes.Buffer
	req := strings.NewReader("protocol=https\nhost=github.com\npath=tidbcloud/cloud-storage-engine.git\n\n")
	if err := run(context.Background(), []string{"get"}, req, &stdout, &stderr); err != nil {
		t.Fatalf("run: %v", err)
	}
	if got, want := stdout.String(), "username=x-access-token\npassword=ghs_cli\n"; got != want {
		t.Errorf("stdout = %q, want %q", got, want)
	}
}

func TestRunGetFailsOnBrokerError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer server.Close()
	prepareEnv(t, server.URL)

	var stdout, stderr bytes.Buffer
	req := strings.NewReader("protocol=https\nhost=github.com\npath=pingcap/tidb.git\n\n")
	err := run(context.Background(), []string{"get"}, req, &stdout, &stderr)
	if err == nil {
		t.Fatal("run succeeded, want an error")
	}
	if stdout.Len() != 0 {
		t.Errorf("stdout = %q, want no credentials", stdout.String())
	}
}

func TestRunWithConfigFile(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"token":"ghs_file","expires_at":"2026-10-27T12:55:00Z"}`)
	}))
	defer server.Close()

	dir := t.TempDir()
	caller := filepath.Join(dir, "sa.token")
	if err := os.WriteFile(caller, []byte("caller-token\n"), 0o600); err != nil {
		t.Fatalf("write caller token: %v", err)
	}
	configPath := filepath.Join(dir, "config.json")
	content := `{"broker_url":"` + server.URL + `","token_file":"` + caller + `","cache_dir":"` + filepath.Join(dir, "cache") + `","permissions":{"contents":"read"}}`
	if err := os.WriteFile(configPath, []byte(content), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	var stdout, stderr bytes.Buffer
	req := strings.NewReader("protocol=https\nhost=github.com\npath=pingcap/tidb.git\n\n")
	if err := run(context.Background(), []string{"-config", configPath, "get"}, req, &stdout, &stderr); err != nil {
		t.Fatalf("run: %v", err)
	}
	if got, want := stdout.String(), "username=x-access-token\npassword=ghs_file\n"; got != want {
		t.Errorf("stdout = %q, want %q", got, want)
	}
}

func TestRunRejectsUnexpectedArguments(t *testing.T) {
	prepareEnv(t, "http://127.0.0.1:1")
	var stdout, stderr bytes.Buffer
	err := run(context.Background(), []string{"--verbose", "get"}, strings.NewReader(""), &stdout, &stderr)
	if err == nil || !strings.Contains(err.Error(), "parse helper flags") {
		t.Fatalf("run error = %v, want a flag parsing error", err)
	}
}

func TestRunStoreAndEraseAreNoOps(t *testing.T) {
	prepareEnv(t, "http://127.0.0.1:1")
	for _, action := range []string{"store", "erase"} {
		var stdout, stderr bytes.Buffer
		req := strings.NewReader("protocol=https\nhost=github.com\npath=pingcap/tidb.git\n\n")
		if err := run(context.Background(), []string{action}, req, &stdout, &stderr); err != nil {
			t.Errorf("run(%s): %v", action, err)
		}
		if stdout.Len() != 0 {
			t.Errorf("run(%s) stdout = %q, want empty", action, stdout.String())
		}
	}
}

func TestRunRejectsUnknownOperation(t *testing.T) {
	prepareEnv(t, "http://127.0.0.1:1")
	var stdout, stderr bytes.Buffer
	err := run(context.Background(), []string{"explode"}, strings.NewReader(""), &stdout, &stderr)
	if err == nil || !strings.Contains(err.Error(), "unsupported operation") {
		t.Fatalf("run error = %v, want an unsupported operation error", err)
	}
}

func TestRunRequiresOneOperation(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if err := run(context.Background(), nil, strings.NewReader(""), &stdout, &stderr); err == nil {
		t.Fatal("run without arguments succeeded, want error")
	}
	if err := run(context.Background(), []string{"get", "erase"}, strings.NewReader(""), &stdout, &stderr); err == nil {
		t.Fatal("run with two arguments succeeded, want error")
	}
}
