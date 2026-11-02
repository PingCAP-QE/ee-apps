package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/google/go-containerregistry/pkg/crane"
	v1 "github.com/google/go-containerregistry/pkg/v1"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

const (
	ociLabelSource   = "org.opencontainers.image.source"
	ociLabelRevision = "org.opencontainers.image.revision"
	ociLabelRefName  = "org.opencontainers.image.ref.name"

	tcmsImageBuildPath = "/tidbx-component-image-builds"
)

// AddTidbxImageTagInTcms records a TiDB-X component image build in TCMS.
func (s *tidbcloudsrvc) AddTidbxImageTagInTcms(ctx context.Context, p *tidbcloud.AddTidbxImageTagInTcmsPayload) (*tidbcloud.AddTidbxImageTagInTcmsResult, error) {
	if p == nil {
		return nil, fmt.Errorf("payload is nil")
	}

	_, imageTag, err := parseImageRepoTag(p.Image)
	if err != nil {
		return nil, fmt.Errorf("bad image param: %w", err)
	}

	tpsCfg := s.testPlatformsConfig()
	if tpsCfg == nil || tpsCfg.TCMS.APIBaseURL == "" {
		return nil, fmt.Errorf("tidbcloud test platforms config is not configured")
	}

	s.populateGithubFromImage(ctx, p)

	reqBody := &tidbcloud.AddTidbxImageTagInTcmsResult{ImageTag: &imageTag}
	if p.Github != nil {
		repo, sha := p.Github.FullRepo, p.Github.CommitSha
		reqBody.Repo = &repo
		reqBody.Sha = &sha
		// ref is optional; a nil ref must not panic and must not send an empty
		// branch.
		if p.Github.Ref != nil {
			branch := strings.TrimPrefix(*p.Github.Ref, "refs/heads/")
			branch = strings.TrimPrefix(branch, "refs/tags/")
			reqBody.Branch = &branch
		}
	}

	headers := map[string]string{}
	if tpsCfg.TCMS.AuthToken != "" {
		headers["Authorization"] = "Bearer " + tpsCfg.TCMS.AuthToken
	}

	status, body, err := s.postJSON(ctx, strings.TrimSuffix(tpsCfg.TCMS.APIBaseURL, "/")+tcmsImageBuildPath, headers, reqBody)
	if err != nil {
		s.logger.Err(err).Msg("call tcms api failed")
		return nil, err
	}
	if status < 200 || status >= 300 {
		s.logger.Error().Int("status", status).Str("resp", string(body)).Msg("call tcms api failed")
		return nil, fmt.Errorf("call tcms api failed: %s", strings.TrimSpace(string(body)))
	}
	s.logger.Info().Str("response", strings.TrimSpace(string(body))).Msg("tcms response successfully")

	return reqBody, nil
}

// populateGithubFromImage fills the git information from the image OCI labels
// when the caller did not supply it.
func (s *tidbcloudsrvc) populateGithubFromImage(ctx context.Context, p *tidbcloud.AddTidbxImageTagInTcmsPayload) {
	if p == nil || p.Github != nil {
		return
	}

	readConfig := s.readImageConfig
	if readConfig == nil {
		readConfig = func(ctx context.Context, image string) ([]byte, error) {
			return crane.Config(image, crane.WithContext(ctx))
		}
	}
	configBytes, err := readConfig(ctx, p.Image)
	if err != nil {
		s.logger.Err(err).Msg("read image config labels failed")
		return
	}

	var config v1.ConfigFile
	if err := json.Unmarshal(configBytes, &config); err != nil {
		s.logger.Err(err).Msg("unmarshal image config failed")
		return
	}
	if config.Config.Labels == nil {
		return
	}

	labels := config.Config.Labels
	repo := githubRepoFromSource(labels[ociLabelSource])
	sha := strings.TrimSpace(labels[ociLabelRevision])
	ref := strings.TrimSpace(labels[ociLabelRefName])
	if repo == "" || sha == "" {
		return
	}

	var refPtr *string
	if ref != "" {
		refPtr = &ref
	}

	p.Github = &struct {
		FullRepo  string
		Ref       *string
		CommitSha string
	}{
		FullRepo:  repo,
		Ref:       refPtr,
		CommitSha: sha,
	}
}

// githubRepoFromSource normalizes the org.opencontainers.image.source label to
// "<org>/<repo>".
func githubRepoFromSource(source string) string {
	src := strings.TrimSpace(source)
	if src == "" {
		return ""
	}

	if after, ok := strings.CutPrefix(src, "git@github.com:"); ok {
		src = after
		return strings.TrimSuffix(src, ".git")
	}
	if after, ok := strings.CutPrefix(src, "ssh://git@github.com/"); ok {
		src = after
		return strings.TrimSuffix(src, ".git")
	}

	if strings.HasPrefix(src, "http://") || strings.HasPrefix(src, "https://") {
		parsed, err := url.Parse(src)
		if err != nil || parsed.Host != "github.com" {
			return ""
		}
		src = strings.TrimPrefix(parsed.Path, "/")
	} else if before, ok := strings.CutPrefix(src, "github.com/"); ok {
		src = before
	} else {
		return ""
	}

	src = strings.TrimSuffix(src, ".git")
	parts := strings.Split(src, "/")
	if len(parts) < 2 {
		return ""
	}
	return parts[0] + "/" + parts[1]
}
