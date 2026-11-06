package impl

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/rs/zerolog"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// The tidbcloud integration talks to two upstreams:
//
//   - the TiDB Cloud ops platform (per-stage base URL + x-api-key), which
//     updates component versions and accepts kernel image build callbacks;
//   - TCMS (base URL + bearer token), which records TiDB-X component builds.
//
// Both configs are mounted from a Secret and loaded once at startup, then
// reloaded when the configuration file changes. The API keys never appear in
// the main config document.

// OpsConfig is the ops platform configuration (per stage).
type OpsConfig struct {
	Stages map[string]OpsStageConfig `json:"stages" yaml:"stages"`
}

// OpsStageConfig is one ops environment.
type OpsStageConfig struct {
	APIBaseURL string                  `json:"api_base_url" yaml:"api_base_url"`
	APIKey     string                  `json:"api_key" yaml:"api_key"`
	Components map[string]OpsComponent `json:"components" yaml:"components"`
}

// OpsComponent maps an image repository to a TiDB Cloud component.
type OpsComponent struct {
	BaseImage   string `json:"base_image" yaml:"base_image"`
	ClusterType string `json:"cluster_type" yaml:"cluster_type"`
	GitHubRepo  string `json:"github_repo" yaml:"github_repo"`
}

// OpsUpdateComponentRequest is the body of the ops component update.
type OpsUpdateComponentRequest struct {
	ClusterType string `json:"cluster_type"`
	Version     string `json:"version"`
	BaseImage   string `json:"base_image"`
	Tag         string `json:"tag"`
	Policy      string `json:"policy"`
	Author      string `json:"author,omitempty"`
	ReleaseID   string `json:"release_id,omitempty"`
	ChangeID    string `json:"change_id,omitempty"`
}

// OpsUpdateComponentResponse is the ops response with the ticket id.
type OpsUpdateComponentResponse struct {
	InstanceID int `json:"instance_id"`
}

// OpsKernelImageSyncRequest is the ops kernel image build callback body.
type OpsKernelImageSyncRequest struct {
	SourceApplicant string                           `json:"source_applicant"`
	SourceReleaseID string                           `json:"source_release_id,omitempty"`
	SourceChangeID  string                           `json:"source_change_id,omitempty"`
	Repo            string                           `json:"repo"`
	Branch          string                           `json:"branch,omitempty"`
	CommitSHA       string                           `json:"commit_sha"`
	GitTag          string                           `json:"git_tag,omitempty"`
	Images          []OpsKernelImageSyncRequestImage `json:"images"`
}

// OpsKernelImageSyncRequestImage is one source image in the callback body.
type OpsKernelImageSyncRequestImage struct {
	SourceRepository string `json:"source_repository"`
	SourceTag        string `json:"source_tag"`
}

// TagMetadata is the author and ops request ids attached to a tidbx git tag.
type TagMetadata struct {
	Author    string
	ReleaseID string
	ChangeID  string
}

// TagMetadataResolver resolves a tag's metadata. TiBuild queries its own hotfix
// service in process, so the integration does not call back over HTTP.
type TagMetadataResolver func(ctx context.Context, repo, tag string) (*TagMetadata, error)

// TestPlatformsConfig is the TCMS configuration.
type TestPlatformsConfig struct {
	TCMS TCMSConfig `json:"tcms" yaml:"tcms"`
}

// TCMSConfig is the TCMS API configuration.
type TCMSConfig struct {
	APIBaseURL string `json:"api_base_url" yaml:"api_base_url"`
	AuthToken  string `json:"auth_token" yaml:"auth_token"`
}

type tidbcloudsrvc struct {
	logger *zerolog.Logger

	mu     sync.RWMutex
	opsCfg *OpsConfig
	tpsCfg *TestPlatformsConfig

	httpClient *http.Client
	readFile   func(string) ([]byte, error)
	// readImageConfig returns the OCI image config JSON. It is a field so tests
	// can replay recorded labels without a registry.
	readImageConfig func(context.Context, string) ([]byte, error)

	// kernelImageMetaReader overrides the default OCI label reader (tests).
	kernelImageMetaReader kernelImageMetaReader
	// resolveTagMetadata resolves git tag metadata in process; nil skips it.
	resolveTagMetadata TagMetadataResolver
}

// NewTidbcloud returns the tidbcloud service implementation.
func NewTidbcloud(logger *zerolog.Logger, cfg *config.Service, resolveTagMetadata TagMetadataResolver) tidbcloud.Service {
	s := &tidbcloudsrvc{
		logger:             logger,
		resolveTagMetadata: resolveTagMetadata,
		httpClient:         &http.Client{Timeout: 30 * time.Second},
		readFile:           os.ReadFile,
		readImageConfig: func(ctx context.Context, image string) ([]byte, error) {
			return crane.Config(image, crane.WithContext(ctx))
		},
	}
	if err := s.applyConfig(cfg); err != nil {
		logger.Fatal().Err(err).Msg("failed to load tidbcloud configuration")
	}
	return s
}

// Reload re-reads the ops and TCMS config files. A failed reload keeps the
// previous snapshot, so a bad Secret update cannot take the integration down.
func (s *tidbcloudsrvc) Reload(cfg *config.Service) {
	if err := s.applyConfig(cfg); err != nil {
		s.logger.Err(err).Msg("failed to reload tidbcloud configuration")
	}
}

// applyConfig parses both config files into locals and swaps them in only
// after both parsed successfully.
func (s *tidbcloudsrvc) applyConfig(cfg *config.Service) error {
	if cfg == nil {
		return nil
	}

	var opsCfg *OpsConfig
	if path := cfg.Tidbcloud.OpsConfigFile; path != "" {
		raw, err := s.readFile(path)
		if err != nil {
			return fmt.Errorf("read tidbcloud ops config %q: %w", path, err)
		}
		var parsed OpsConfig
		if err := yaml.Unmarshal(raw, &parsed); err != nil {
			return fmt.Errorf("parse tidbcloud ops config %q: %w", path, err)
		}
		if len(parsed.Stages) == 0 {
			return fmt.Errorf("tidbcloud ops config %q has no stages", path)
		}
		opsCfg = &parsed
	}

	var tpsCfg *TestPlatformsConfig
	if path := cfg.Tidbcloud.TestPlatformsConfigFile; path != "" {
		raw, err := s.readFile(path)
		if err != nil {
			return fmt.Errorf("read tidbcloud test platforms config %q: %w", path, err)
		}
		var parsed TestPlatformsConfig
		if err := yaml.Unmarshal(raw, &parsed); err != nil {
			return fmt.Errorf("parse tidbcloud test platforms config %q: %w", path, err)
		}
		tpsCfg = &parsed
	}

	s.mu.Lock()
	s.opsCfg = opsCfg
	s.tpsCfg = tpsCfg
	s.mu.Unlock()
	return nil
}

func (s *tidbcloudsrvc) opsConfig() *OpsConfig {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.opsCfg
}

func (s *tidbcloudsrvc) testPlatformsConfig() *TestPlatformsConfig {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.tpsCfg
}

// postJSON posts a JSON body and returns the status code and body. A non-2xx
// response is not an error here; callers decide whether it is fatal so the
// error message can name the upstream.
func (s *tidbcloudsrvc) postJSON(ctx context.Context, url string, headers map[string]string, body any) (int, []byte, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return 0, nil, fmt.Errorf("encode request body: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(raw))
	if err != nil {
		return 0, nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp.StatusCode, nil, fmt.Errorf("read response: %w", err)
	}
	return resp.StatusCode, respBody, nil
}

// parseImageRepoTag splits an image reference into repository and tag. A
// registry port does not confuse the split because the tag part never contains
// a slash.
func parseImageRepoTag(image string) (string, string, error) {
	image = strings.TrimSpace(image)
	if image == "" {
		return "", "", fmt.Errorf("image is empty")
	}
	if idx := strings.LastIndex(image, "@"); idx >= 0 {
		if idx == len(image)-1 {
			return "", "", fmt.Errorf("invalid image: %s", image)
		}
		return image[:idx], image[idx+1:], nil
	}
	idx := strings.LastIndex(image, ":")
	if idx < 0 || idx == len(image)-1 || strings.Contains(image[idx+1:], "/") {
		return "", "", fmt.Errorf("invalid image: %s", image)
	}
	return image[:idx], image[idx+1:], nil
}
