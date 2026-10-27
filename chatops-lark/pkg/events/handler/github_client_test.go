package handler

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/chatops-lark/pkg/config"
)

func TestNewGitHubClientFactoryDefaultsToInClusterBroker(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{}}, "")
	if f.brokerURL != defaultGitHubBrokerURL {
		t.Fatalf("broker URL = %q, want the in-cluster default", f.brokerURL)
	}
	if f.tokenFile != defaultGitHubBrokerTokenFile {
		t.Fatalf("token file = %q, want the projected ServiceAccount token", f.tokenFile)
	}
}

func TestNewGitHubClientFactoryRespectsExplicitConfig(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{
		BrokerURL:       "http://broker.example",
		BrokerTokenFile: "/tmp/sa-token",
		Token:           "ghp_global",
	}}, "")
	if f.brokerURL != "http://broker.example" {
		t.Fatalf("broker URL = %q", f.brokerURL)
	}
	if f.tokenFile != "/tmp/sa-token" {
		t.Fatalf("token file = %q", f.tokenFile)
	}
	if f.fallbackToken != "ghp_global" {
		t.Fatalf("fallback token = %q", f.fallbackToken)
	}
}

func TestNewGitHubClientFactoryEnvOverrides(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "http://broker.env")
	t.Setenv(envGitHubBrokerTokenFile, "/tmp/env-token")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{
		BrokerURL:       "http://broker.example",
		BrokerTokenFile: "/tmp/sa-token",
	}}, "")
	if f.brokerURL != "http://broker.env" {
		t.Fatalf("broker URL = %q, want the environment override", f.brokerURL)
	}
	if f.tokenFile != "/tmp/env-token" {
		t.Fatalf("token file = %q, want the environment override", f.tokenFile)
	}
}

func TestNewGitHubClientFactoryKeepsStaticOnlyConfig(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{}, "ghp_legacy")
	if f.brokerURL != "" {
		t.Fatalf("broker URL = %q, want the broker disabled when the github block is absent", f.brokerURL)
	}
	if f.fallbackToken != "ghp_legacy" {
		t.Fatalf("fallback token = %q, want the command-level token", f.fallbackToken)
	}
}

func TestNewGitHubClientFactoryPrefersCommandTokenOverGlobal(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{
		Token: "ghp_global",
	}}, "ghp_command")
	if f.fallbackToken != "ghp_command" {
		t.Fatalf("fallback token = %q, want the command-level token", f.fallbackToken)
	}
}

func TestGitHubClientFactoryScopesTransportToRepository(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{
		BrokerURL:       "http://broker.example",
		BrokerTokenFile: "/tmp/sa-token",
		Token:           "ghp_global",
	}}, "")
	got := f.transportConfig("pingcap/tidb", repoAdminPermissions)

	if got.BrokerURL != "http://broker.example" {
		t.Fatalf("BrokerURL = %q", got.BrokerURL)
	}
	if got.TokenFile != "/tmp/sa-token" {
		t.Fatalf("TokenFile = %q", got.TokenFile)
	}
	if got.FallbackToken != "ghp_global" {
		t.Fatalf("FallbackToken = %q", got.FallbackToken)
	}
	if got.Repo != "pingcap/tidb" {
		t.Fatalf("Repo = %q", got.Repo)
	}
	if !reflect.DeepEqual(got.Permissions, repoAdminPermissions) {
		t.Fatalf("Permissions = %#v, want %#v", got.Permissions, repoAdminPermissions)
	}
	if got.OnFallback == nil {
		t.Fatal("OnFallback is nil, fallback-to-PAT events would be silent")
	}
}

func TestGitHubClientFactoryOnFallbackLogsWarning(t *testing.T) {
	var buf bytes.Buffer
	original := log.Logger
	log.Logger = zerolog.New(&buf).Level(zerolog.DebugLevel)
	t.Cleanup(func() { log.Logger = original })

	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{}}, "")
	f.transportConfig("pingcap/tidb", repoAdminPermissions).OnFallback("pingcap/tidb", errors.New("token broker returned HTTP 403"))

	out := buf.String()
	if !bytes.Contains([]byte(out), []byte("fallback")) {
		t.Fatalf("fallback log = %q, want a warning mentioning the fallback token", out)
	}
	if !bytes.Contains([]byte(out), []byte("pingcap/tidb")) {
		t.Fatalf("fallback log = %q, want the repository", out)
	}
	if bytes.Contains([]byte(out), []byte("ghp_")) {
		t.Fatalf("fallback log leaks a token: %q", out)
	}
}

func TestGitHubClientFactoryWritesTokenFileDefault(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{}}, "")
	client := f.newClient("pingcap/tidb", repoAdminPermissions)
	if client == nil {
		t.Fatal("client is nil")
	}
}

func TestBrokerTokenFileDefaultPath(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "token")
	if err := os.WriteFile(path, []byte("sa"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(envGitHubBrokerTokenFile, path)

	f := newGitHubClientFactory(config.Config{GitHub: &config.GitHubConfig{}}, "")
	if f.tokenFile != path {
		t.Fatalf("token file = %q, want %q", f.tokenFile, path)
	}
}

func TestSetupCtxRepoAdminStoresProvider(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	cfg := config.Config{
		GitHub: &config.GitHubConfig{BrokerURL: "http://broker.example"},
		RepoAdmin: &struct {
			config.BaseCmdConfig `yaml:",inline" json:",inline"`
			GithubToken          string `yaml:"github_token" json:"github_token"`
		}{GithubToken: "ghp_legacy"},
	}

	ctx := setupCtxRepoAdmin(context.Background(), cfg, nil)
	provider, err := gitHubClientFromContext(ctx)
	if err != nil {
		t.Fatalf("gitHubClientFromContext() error = %v", err)
	}
	factory, ok := provider.(githubClientFactory)
	if !ok {
		t.Fatalf("provider type = %T, want githubClientFactory", provider)
	}
	if factory.brokerURL != "http://broker.example" {
		t.Fatalf("broker URL = %q", factory.brokerURL)
	}
	if factory.fallbackToken != "ghp_legacy" {
		t.Fatalf("fallback token = %q", factory.fallbackToken)
	}
}

func TestSetupCtxRegistryImageStoresProviderAndDefaults(t *testing.T) {
	t.Setenv(envGitHubBrokerURL, "")
	t.Setenv(envGitHubBrokerTokenFile, "")

	cfg := config.Config{
		GitHub: &config.GitHubConfig{},
		RegistryImage: &config.RegistryImageConfig{
			Ref:         " main ",
			GitHubToken: "ghp_legacy",
		},
	}

	ctx := setupCtxRegistryImage(context.Background(), cfg, nil)
	provider, err := gitHubClientFromContext(ctx)
	if err != nil {
		t.Fatalf("gitHubClientFromContext() error = %v", err)
	}
	if factory, ok := provider.(githubClientFactory); !ok || factory.fallbackToken != "ghp_legacy" {
		t.Fatalf("provider = %#v, want the command fallback token", provider)
	}

	workflowCfg, err := loadRegistryImageWorkflowConfig(ctx)
	if err != nil {
		t.Fatalf("loadRegistryImageWorkflowConfig() error = %v", err)
	}
	if workflowCfg.Owner != "tidbcloud" || workflowCfg.Repo != "docker-image-controller" || workflowCfg.Workflow != "query-image-tag.yml" {
		t.Fatalf("workflow config = %#v, want the documented defaults", workflowCfg)
	}
	if workflowCfg.Ref != "main" {
		t.Fatalf("ref = %q, want the trimmed configured ref", workflowCfg.Ref)
	}
}

func TestLoadRegistryImageWorkflowConfigRejectsMissingContext(t *testing.T) {
	if _, err := loadRegistryImageWorkflowConfig(context.Background()); err == nil {
		t.Fatal("expected an error when the registry image context is missing")
	}
}
