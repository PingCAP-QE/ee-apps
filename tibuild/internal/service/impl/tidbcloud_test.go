package impl

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// newTidbcloudTestService builds the service from YAML files, the same way the
// deployment mounts them, so config loading is part of the test.
func newTidbcloudTestService(t *testing.T, ops *OpsConfig, tps *TestPlatformsConfig) *tidbcloudsrvc {
	t.Helper()

	dir := t.TempDir()
	cfg := &config.Service{}
	if ops != nil {
		path := filepath.Join(dir, "ops.yaml")
		raw, err := yaml.Marshal(ops)
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(path, raw, 0o600))
		cfg.Tidbcloud.OpsConfigFile = path
	}
	if tps != nil {
		path := filepath.Join(dir, "testplatforms.yaml")
		raw, err := yaml.Marshal(tps)
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(path, raw, 0o600))
		cfg.Tidbcloud.TestPlatformsConfigFile = path
	}

	// Kernel-image sync fails closed on an empty allowlist, so the fixture has
	// to name the repositories its cases use.
	cfg.Tidbcloud.KernelImageAllowedRepositories = []string{
		"us.gcr.io/pingcap-public/tidbx/*",
		"hub.pingcap.net/*",
		"example.com/*",
	}

	logger := zerolog.Nop()
	svc := &tidbcloudsrvc{
		logger:     &logger,
		httpClient: &http.Client{Timeout: 5 * time.Second},
		readFile:   os.ReadFile,
		// Rollouts verify the tag exists; cases that do not exercise that check
		// get a resolver that always succeeds.
		resolveImageTag: func(context.Context, string) (string, error) {
			return "sha256:" + strings.Repeat("a", 64), nil
		},
	}
	require.NoError(t, svc.applyConfig(cfg))
	return svc
}

func TestParseImageRepoTag(t *testing.T) {
	tests := []struct {
		image string
		repo  string
		tag   string
		ok    bool
	}{
		{image: "us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4", repo: "us.gcr.io/pingcap-public/tidbx/tikv", tag: "v8.5.4", ok: true},
		{image: "registry.example.com:5000/team/app:v1.2.3", repo: "registry.example.com:5000/team/app", tag: "v1.2.3", ok: true},
		{image: "busybox", ok: false},
		{image: "registry.example.com:5000/team/app", ok: false},
		{image: "a/b@sha256:abc", repo: "a/b", tag: "sha256:abc", ok: true},
		{image: "", ok: false},
	}
	for _, tt := range tests {
		repo, tag, err := parseImageRepoTag(tt.image)
		if !tt.ok {
			require.Error(t, err, tt.image)
			continue
		}
		require.NoError(t, err, tt.image)
		require.Equal(t, tt.repo, repo, tt.image)
		require.Equal(t, tt.tag, tag, tt.image)
	}
}

func TestGitHubRepoFromSource(t *testing.T) {
	tests := map[string]string{
		"https://github.com/pingcap/tidb":           "pingcap/tidb",
		"https://github.com/pingcap/tidb.git":       "pingcap/tidb",
		"git@github.com:pingcap/tidb.git":           "pingcap/tidb",
		"ssh://git@github.com/pingcap/tidb.git":     "pingcap/tidb",
		"github.com/pingcap/tidb":                   "pingcap/tidb",
		"https://gitlab.com/pingcap/tidb":           "",
		"":                                          "",
		"https://github.com/pingcap/tidb/tree/main": "pingcap/tidb",
	}
	for in, want := range tests {
		require.Equal(t, want, githubRepoFromSource(in), in)
	}
}

func TestIsSupportedNextgenImageTag(t *testing.T) {
	supported := []string{
		"v7.5.0-nextgen.240101.1",
		"v26.0.0-nextgen",
		"v26.3.1",
		"v26.3.1-nextgen",
	}
	unsupported := []string{
		"master",
		"v25.9.9-nextgen",
		"v8.5.9-pre",
		"v26.3.1-nextgen.20250101.1",
	}
	for _, tag := range supported {
		require.True(t, isSupportedNextgenImageTag(tag), tag)
	}
	for _, tag := range unsupported {
		require.False(t, isSupportedNextgenImageTag(tag), tag)
	}
}

func TestNormalizeComponentVersion(t *testing.T) {
	require.Equal(t, "v26.3.99", normalizeComponentVersion("v26.3.1"))
	require.Equal(t, "v26.3.99", normalizeComponentVersion("v26.3.42"))
	require.Equal(t, "v26.4.1", normalizeComponentVersion("v26.4.1"))
}

func TestClassifyKernelImageRef(t *testing.T) {
	tag, branch := classifyKernelImageRef("v8.5.4")
	require.Equal(t, "v8.5.4", tag)
	require.Empty(t, branch)

	tag, branch = classifyKernelImageRef("release-8.5")
	require.Empty(t, tag)
	require.Equal(t, "release-8.5", branch)

	tag, branch = classifyKernelImageRef("")
	require.Empty(t, tag)
	require.Empty(t, branch)

	// Builds record the full refspec for tag pushes; the namespace must not leak
	// into the branch name.
	tag, branch = classifyKernelImageRef("refs/tags/v26.3.21-nextgen")
	require.Equal(t, "v26.3.21-nextgen", tag)
	require.Empty(t, branch)

	tag, branch = classifyKernelImageRef("refs/heads/release-nextgen-202603")
	require.Empty(t, tag)
	require.Equal(t, "release-nextgen-202603", branch)
}

func TestNewTidbcloudLoadsConfigAndReload(t *testing.T) {
	dir := t.TempDir()
	opsPath := filepath.Join(dir, "ops.yaml")
	writeOps := func(baseURL string) {
		raw, err := yaml.Marshal(&OpsConfig{
			Stages: map[string]OpsStageConfig{"prod": {APIBaseURL: baseURL}},
		})
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(opsPath, raw, 0o600))
	}

	writeOps("http://old")
	cfg := &config.Service{Tidbcloud: config.Tidbcloud{OpsConfigFile: opsPath}}
	logger := zerolog.Nop()
	svc := NewTidbcloud(&logger, cfg, nil).(*tidbcloudsrvc)
	require.Equal(t, "http://old", svc.opsConfig().Stages["prod"].APIBaseURL)

	writeOps("http://new")
	svc.Reload(cfg)
	require.Equal(t, "http://new", svc.opsConfig().Stages["prod"].APIBaseURL)

	// A broken revision keeps the last good config.
	require.NoError(t, os.WriteFile(opsPath, []byte("stages: ["), 0o600))
	svc.Reload(cfg)
	require.Equal(t, "http://new", svc.opsConfig().Stages["prod"].APIBaseURL)
}

func TestReadImageConfigLabels(t *testing.T) {
	const sha = "031069dfc0c70e839d996c9e1cf3d34930fc662f"
	configJSON := []byte(`{"config":{"Labels":{
		"org.opencontainers.image.source":"https://github.com/pingcap/tidb",
		"org.opencontainers.image.revision":"` + sha + `",
		"org.opencontainers.image.ref.name":"refs/heads/main"
	}}}`)

	svc := newTidbcloudTestService(t, nil, nil)
	svc.readImageConfig = func(context.Context, string) ([]byte, error) { return configJSON, nil }

	meta := svc.readKernelImageMeta(context.Background(), "example.com/tidb:v8.5.4")
	require.Equal(t, "pingcap/tidb", meta.Repo)
	require.Equal(t, sha, meta.CommitSHA)
	// The ref namespace is stripped so the ops payload carries a branch name.
	require.Equal(t, "main", meta.Branch)
	require.Empty(t, meta.GitTag)

	p := &tidbcloud.AddTidbxImageTagInTcmsPayload{Image: "example.com/tidb:v8.5.4"}
	svc.populateGithubFromLabels(p, svc.readImageLabels(context.Background(), p.Image))
	require.NotNil(t, p.Github)
	require.Equal(t, "pingcap/tidb", p.Github.FullRepo)
	require.Equal(t, sha, p.Github.CommitSha)
	require.Equal(t, "refs/heads/main", *p.Github.Ref)
}

func TestRequestSyncKernelImageNilPayload(t *testing.T) {
	svc := newTidbcloudTestService(t, nil, nil)
	_, err := svc.RequestSyncKernelImage(context.Background(), nil)
	require.ErrorContains(t, err, "payload is nil")
}
