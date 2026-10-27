package gitcred

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConfigFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	content := `{
  "broker_url": "http://broker.example",
  "cache_dir": "/tmp/cache",
  "permissions": {"contents": "read"},
  "hosts": ["github.com"],
  "fallback_credentials_file": "/tekton/home/.git-credentials",
  "state_file": "/tmp/state",
  "timeout": "7s"
}`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := ConfigFromFile(path)
	if err != nil {
		t.Fatalf("ConfigFromFile: %v", err)
	}
	if cfg.BrokerURL != "http://broker.example" {
		t.Errorf("broker URL = %q", cfg.BrokerURL)
	}
	if cfg.TokenFile != defaultCallerTokenFile {
		t.Errorf("token file = %q, want the in-cluster default", cfg.TokenFile)
	}
	if cfg.CacheDir != "/tmp/cache" || cfg.StateFile != "/tmp/state" {
		t.Errorf("paths = %q, %q", cfg.CacheDir, cfg.StateFile)
	}
	if cfg.Permissions["contents"] != "read" {
		t.Errorf("permissions = %v", cfg.Permissions)
	}
	if len(cfg.Hosts) != 1 || cfg.Hosts[0] != "github.com" {
		t.Errorf("hosts = %v", cfg.Hosts)
	}
	if cfg.FallbackCredentialsFile != "/tekton/home/.git-credentials" {
		t.Errorf("fallback = %q", cfg.FallbackCredentialsFile)
	}
	if cfg.Timeout.String() != "7s" {
		t.Errorf("timeout = %v", cfg.Timeout)
	}
}

func TestConfigFromFileRejectsUnknownFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, []byte(`{"broker_url":"x","unknown":1}`), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	if _, err := ConfigFromFile(path); err == nil {
		t.Fatal("ConfigFromFile accepted an unknown field, want error")
	}
}

func TestConfigFromFileRejectsBadTimeout(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, []byte(`{"broker_url":"x","timeout":"soon"}`), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	if _, err := ConfigFromFile(path); err == nil {
		t.Fatal("ConfigFromFile accepted a bad timeout, want error")
	}
}

func TestConfigFromFileMissingFile(t *testing.T) {
	if _, err := ConfigFromFile(filepath.Join(t.TempDir(), "absent.json")); err == nil {
		t.Fatal("ConfigFromFile succeeded for a missing file, want error")
	}
}
