package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadConfigGitHubBroker(t *testing.T) {
	path := writeConfig(t, `
app_id: app
app_secret: secret
github:
  broker_url: http://github-token-broker.github-token-broker.svc
  broker_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token
  token: ghp_fallback
repo_admins:
  github_token: ghp_legacy
`)

	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.GitHub == nil {
		t.Fatal("github config is nil")
	}
	if cfg.GitHub.BrokerURL != "http://github-token-broker.github-token-broker.svc" {
		t.Fatalf("broker_url = %q", cfg.GitHub.BrokerURL)
	}
	if cfg.GitHub.BrokerTokenFile != "/var/run/secrets/kubernetes.io/serviceaccount/token" {
		t.Fatalf("broker_token_file = %q", cfg.GitHub.BrokerTokenFile)
	}
	if cfg.GitHub.Token != "ghp_fallback" {
		t.Fatalf("token = %q", cfg.GitHub.Token)
	}
	if cfg.RepoAdmin == nil || cfg.RepoAdmin.GithubToken != "ghp_legacy" {
		t.Fatalf("legacy repo_admins.github_token was not parsed")
	}
}

func TestLoadConfigWithoutGitHubBlock(t *testing.T) {
	path := writeConfig(t, `
app_id: app
app_secret: secret
repo_admins:
  github_token: ghp_legacy
`)

	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.GitHub != nil {
		t.Fatalf("github config = %#v, want nil when the block is absent", cfg.GitHub)
	}
}
