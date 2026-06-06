package tidbcloud

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/go-resty/resty/v2"
	"github.com/google/go-containerregistry/pkg/crane"
	v1 "github.com/google/go-containerregistry/pkg/v1"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
)

const (
	ociLabelSource   = "org.opencontainers.image.source"
	ociLabelRevision = "org.opencontainers.image.revision"
	ociLabelRefName  = "org.opencontainers.image.ref.name"
)

// CreateTidbxComponentImageBuild implements create-tidbx-component-image-build.
func (s *tidbcloudsrvc) AddTidbxImageTagInTcms(ctx context.Context, p *tidbcloud.AddTidbxImageTagInTcmsPayload) (res *tidbcloud.AddTidbxImageTagInTcmsResult, err error) {
	if p == nil {
		return nil, fmt.Errorf("payload is nil")
	}

	_, imgTag, err := parseImageRepoTag(p.Image)
	if err != nil {
		return nil, fmt.Errorf("bad image param")
	}

	s.populateGithubFromImage(ctx, p)

	reqBody := &tidbcloud.AddTidbxImageTagInTcmsResult{
		ImageTag: &imgTag,
	}
	if p.Github != nil {
		branch := strings.TrimPrefix(*p.Github.Ref, "refs/heads/")
		branch = strings.TrimPrefix(branch, "refs/tags/")
		reqBody.Branch = &branch
		reqBody.Repo = &p.Github.FullRepo
		reqBody.Sha = &p.Github.CommitSha
	}

	resp, err := s.tcmsRestyClient().R().
		SetContext(ctx).
		SetBody(reqBody).
		Post("/tidbx-component-image-builds")
	if err != nil {
		body := ""
		if resp != nil {
			body = string(resp.Body())
		}
		// s.Logger.Info().Any("headers", resp.Request.RawRequest.Header).Msg("debug")
		s.Logger.Err(err).Str("resp", body).Msg("call tcms api failed")
		return nil, err
	}
	if resp.IsError() {
		respBytes := resp.Body()
		s.Logger.Error().Str("resp", string(respBytes)).Msg("call tcms api failed")
		return nil, fmt.Errorf("call tcms api failed: %s", respBytes)
	}
	s.Logger.Info().Bytes("resonse", resp.Body()).Msg("tcms response successfully")

	return reqBody, nil
}

func (s *tidbcloudsrvc) populateGithubFromImage(ctx context.Context, p *tidbcloud.AddTidbxImageTagInTcmsPayload) {
	if p == nil || p.Github != nil {
		return
	}

	configBytes, err := crane.Config(p.Image, crane.WithContext(ctx))
	if err != nil {
		s.Logger.Err(err).Msg("read image config labels failed")
		return
	}

	var config v1.ConfigFile
	if err := json.Unmarshal(configBytes, &config); err != nil {
		s.Logger.Err(err).Msg("unmarshal image config failed")
		return
	}
	if config.Config.Labels == nil {
		return
	}

	labels := config.Config.Labels
	s.Logger.Info().Any("labels", labels).Msg("image labels")
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

func (s *tidbcloudsrvc) tcmsRestyClient() *resty.Client {
	cfg := s.tpsCfg.TCMS
	client := resty.New().
		SetBaseURL(cfg.APIBaseURL).
		SetAuthToken(cfg.AuthToken).
		SetTimeout(10 * time.Second)
	return client
}
