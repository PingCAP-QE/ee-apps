package config

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeConfig(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const minimalConfig = `
github:
  app_id: 123456
  private_key_path: /tmp/key.pem
policy:
  rules:
    - caller:
        namespace: ci
        service_account: checkout
      allow:
        - owner: example-org
          repositories: ["*"]
          max_permissions:
            contents: read
`

func TestLoadDefaults(t *testing.T) {
	cfg, err := Load(writeConfig(t, minimalConfig))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Server.Address != ":8080" {
		t.Fatalf("unexpected address: %s", cfg.Server.Address)
	}
	if cfg.Server.ReadTimeout != 10*time.Second || cfg.Server.WriteTimeout != 30*time.Second {
		t.Fatalf("unexpected server timeouts: %#v", cfg.Server)
	}
	if cfg.GitHub.APIURL != "https://api.github.com" {
		t.Fatalf("unexpected api url: %s", cfg.GitHub.APIURL)
	}
	if cfg.Cache.TTLMargin != 5*time.Minute || cfg.Cache.MaxEntries != 1000 {
		t.Fatalf("unexpected cache defaults: %#v", cfg.Cache)
	}
	if cfg.RateLimit.PerMinute != 120 {
		t.Fatalf("unexpected rate limit default: %#v", cfg.RateLimit)
	}
	if cfg.Reload.Disabled || cfg.Reload.Interval != 5*time.Second {
		t.Fatalf("unexpected reload defaults: %#v", cfg.Reload)
	}
	if cfg.EffectivePolicy() == nil {
		t.Fatal("expected an effective policy")
	}
	if cfg.Auth.TokenFile == "" || cfg.Auth.CAFile == "" {
		t.Fatalf("unexpected auth defaults: %#v", cfg.Auth)
	}
	if cfg.HasFallback() {
		t.Fatal("did not expect a fallback PAT")
	}
}

func TestLoadFallbackPATFile(t *testing.T) {
	dir := t.TempDir()
	patPath := filepath.Join(dir, "pat")
	if err := os.WriteFile(patPath, []byte("  ghp_example\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	content := `
github:
  app_id: 123456
  private_key_path: /tmp/key.pem
  fallback_pat_file: ` + patPath + `
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`
	cfg, err := Load(writeConfig(t, content))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.GitHub.FallbackPAT != "ghp_example" {
		t.Fatalf("unexpected fallback PAT: %q", cfg.GitHub.FallbackPAT)
	}
}

func TestLoadInvalid(t *testing.T) {
	tests := []struct {
		name    string
		content string
	}{
		{name: "missing app id", content: `
github:
  private_key_path: /tmp/key.pem
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`},
		{name: "missing private key", content: `
github:
  app_id: 1
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`},
		{name: "unknown field", content: minimalConfig + "\nunknown_field: true\n"},
		{name: "negative rate limit", content: `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
rate_limit:
  per_minute: -1
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`},
		{name: "invalid policy", content: `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
policy:
  rules: []
`},
		{name: "missing fallback file", content: `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
  fallback_pat_file: /does/not/exist
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := Load(writeConfig(t, tt.content)); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestLoadPolicyFile(t *testing.T) {
	dir := t.TempDir()
	policyPath := filepath.Join(dir, "policy.yaml")
	policy := `
rules:
  - caller: {namespace: ci, service_account: checkout}
    allow:
      - owner: example-org
        repositories: ["*"]
        max_permissions: {contents: read}
`
	if err := os.WriteFile(policyPath, []byte(policy), 0o600); err != nil {
		t.Fatal(err)
	}
	configContent := `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
policy:
  file: policy.yaml
`
	configPath := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(configPath, []byte(configContent), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(configPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := len(cfg.EffectivePolicy().Rules); got != 1 {
		t.Fatalf("unexpected policy rules: %d", got)
	}
	if cfg.Policy.File != policyPath {
		t.Fatalf("unexpected policy path: %s", cfg.Policy.File)
	}
}

func TestStoreReload(t *testing.T) {
	path := writeConfig(t, minimalConfig)
	store, err := NewStore(path, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := store.Current().RateLimit.PerMinute; got != 120 {
		t.Fatalf("unexpected initial rate limit: %d", got)
	}

	updated := `
github:
  app_id: 123456
  private_key_path: /tmp/key.pem
rate_limit:
  per_minute: 5
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`
	if err := os.WriteFile(path, []byte(updated), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Reload(); err != nil {
		t.Fatalf("unexpected reload error: %v", err)
	}
	if got := store.Current().RateLimit.PerMinute; got != 5 {
		t.Fatalf("unexpected reloaded rate limit: %d", got)
	}

	if err := os.WriteFile(path, []byte("not: [valid"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Reload(); err == nil {
		t.Fatal("expected a reload error")
	}
	if got := store.Current().RateLimit.PerMinute; got != 5 {
		t.Fatalf("previous configuration was not kept: %d", got)
	}
}

func TestStoreWatch(t *testing.T) {
	path := writeConfig(t, minimalConfig)
	store, err := NewStore(path, nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go store.Watch(ctx, 20*time.Millisecond)
	time.Sleep(100 * time.Millisecond) // let the watcher record the initial digest

	updated := `
github:
  app_id: 123456
  private_key_path: /tmp/key.pem
rate_limit:
  per_minute: 7
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          max_permissions: {contents: read}
`
	if err := os.WriteFile(path, []byte(updated), 0o600); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if store.Current().RateLimit.PerMinute == 7 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("configuration was not reloaded")
}

func TestStoreDigestError(t *testing.T) {
	path := writeConfig(t, minimalConfig)
	store, err := NewStore(path, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := store.digest(); err == nil {
		t.Fatal("expected a digest error")
	}
}

func TestStaticProvider(t *testing.T) {
	cfg := &Config{}
	if got := (StaticProvider{Cfg: cfg}).Current(); got != cfg {
		t.Fatalf("unexpected configuration: %p", got)
	}
}

func TestLoadMissingPolicyFile(t *testing.T) {
	content := `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
policy:
  file: /does/not/exist.yaml
`
	if _, err := Load(writeConfig(t, content)); err == nil {
		t.Fatal("expected an error")
	}
}

func TestLoadInvalidPolicyFile(t *testing.T) {
	dir := t.TempDir()
	policyPath := filepath.Join(dir, "policy.yaml")
	if err := os.WriteFile(policyPath, []byte("rules: []"), 0o600); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(dir, "config.yaml")
	content := `
github:
  app_id: 1
  private_key_path: /tmp/key.pem
policy:
  file: policy.yaml
`
	if err := os.WriteFile(configPath, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(configPath); err == nil {
		t.Fatal("expected an error")
	}
}

func TestLoadReloadDisabled(t *testing.T) {
	content := minimalConfig + `
reload:
  disabled: true
`
	cfg, err := Load(writeConfig(t, content))
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.Reload.Disabled {
		t.Fatal("expected reload to be disabled")
	}
}
