package tidbcloud

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/google/go-containerregistry/pkg/crane"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"golang.org/x/mod/semver"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
)

// kernelImageMeta holds source code metadata read from the source image labels.
type kernelImageMeta struct {
	Repo      string
	Branch    string
	GitTag    string
	CommitSHA string
}

// RequestSyncKernelImage implements request-sync-kernel-image.
func (s *tidbcloudsrvc) RequestSyncKernelImage(ctx context.Context, p *tidbcloud.RequestSyncKernelImagePayload) (res string, err error) {
	if p == nil {
		err = fmt.Errorf("payload is nil")
		s.Logger.Error().Err(err).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}
	if strings.TrimSpace(p.Stage) == "" {
		err = fmt.Errorf("stage is empty")
		s.Logger.Error().Err(err).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}
	if strings.TrimSpace(p.Image) == "" {
		err = fmt.Errorf("image is empty")
		s.Logger.Error().Err(err).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}
	if s.opsCfg == nil {
		err = fmt.Errorf("tidbcloud ops config is not configured")
		s.Logger.Error().Err(err).Str("stage", p.Stage).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}

	sourceRepository, sourceTag, err := parseImageRepoTag(p.Image)
	if err != nil {
		s.Logger.Error().Err(err).Str("source_image", p.Image).Msg("tidbcloud.request-sync-kernel-image failed to parse source image")
		return "", err
	}

	readMeta := s.kernelImageMetaReader
	if readMeta == nil {
		readMeta = s.readKernelImageMeta
	}
	meta := readMeta(ctx, p.Image)
	if meta.Repo == "" || meta.CommitSHA == "" {
		err = fmt.Errorf("failed to read source image metadata: missing repo or commit_sha labels")
		s.Logger.Error().Err(err).Str("source_image", p.Image).Str("repo", meta.Repo).Str("commit_sha", meta.CommitSHA).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}
	if meta.GitTag == "" && meta.Branch == "" {
		err = fmt.Errorf("failed to read source image metadata: missing git ref label")
		s.Logger.Error().Err(err).Str("source_image", p.Image).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}

	client := s.opsRestyClient(p.Stage)
	if client == nil {
		err = fmt.Errorf("ops client is nil for stage %q: stage not found in config", p.Stage)
		s.Logger.Error().Err(err).Str("stage", p.Stage).Msg("requestSyncKernelImage failed")
		return "", err
	}

	// Read applicant/release/change from tibuild tag metadata.
	var sourceApplicant, sourceReleaseID, sourceChangeID string
	md, mdErr := s.getTiBuildTagMetadata(ctx, meta.Repo, sourceTag)
	if mdErr != nil {
		s.Logger.Warn().Err(mdErr).Str("repo", meta.Repo).Str("tag", sourceTag).Msg("failed to get tibuild tag metadata for kernel image")
	} else if md != nil {
		sourceApplicant = md.Author
		sourceReleaseID = md.Meta.OpsReq.ReleaseID
		sourceChangeID = md.Meta.OpsReq.ChangeID
	}
	if sourceApplicant == "" {
		err = fmt.Errorf("source_applicant is empty: unresolvable from tibuild tag metadata")
		s.Logger.Error().Err(err).Str("repo", meta.Repo).Str("tag", sourceTag).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}

	payload := OpsKernelImageSyncRequest{
		SourceApplicant:  sourceApplicant,
		SourceReleaseID:  sourceReleaseID,
		SourceChangeID:   sourceChangeID,
		Repo:             meta.Repo,
		Branch:           meta.Branch,
		CommitSHA:        meta.CommitSHA,
		GitTag:           meta.GitTag,
		SourceRepository: sourceRepository,
		SourceTag:        sourceTag,
	}

	s.Logger.Debug().
		Str("stage", p.Stage).
		Str("source_image", p.Image).
		Str("repo", payload.Repo).
		Str("branch", payload.Branch).
		Str("git_tag", payload.GitTag).
		Str("source_repository", payload.SourceRepository).
		Str("source_tag", payload.SourceTag).
		Msg("calling ops platform kernel image build callback")

	resp, err := client.R().
		SetContext(ctx).
		SetBody(&payload).
		Post("/devops/kernel-images/build-callback")
	if err != nil {
		err = fmt.Errorf("call ops kernel image build callback: %w", err)
		s.Logger.Error().
			Err(err).Int("status", resp.StatusCode()).
			Str("response_body", string(resp.Body())).
			Str("stage", p.Stage).
			Msg("requestSyncKernelImage request failed")
		return "", err
	}
	if resp.IsError() {
		err = fmt.Errorf("call ops kernel image build callback: http status %d: %s", resp.StatusCode(), strings.TrimSpace(string(resp.Body())))
		s.Logger.Error().
			Err(err).
			Int("status", resp.StatusCode()).
			Str("response_body", string(resp.Body())).
			Str("stage", p.Stage).
			Msg("requestSyncKernelImage returned error status")
		return "", err
	}

	s.Logger.Info().
		Str("stage", p.Stage).
		Str("source_image", p.Image).
		Str("repo", payload.Repo).
		Str("branch", payload.Branch).
		Str("git_tag", payload.GitTag).
		Msg("requestSyncKernelImage succeeded")
	return strings.TrimSpace(string(resp.Body())), nil
}

// readKernelImageMeta reads repo, commit SHA and the git ref from the source
// image OCI labels:
//   - repo: org.opencontainers.image.source
//   - commit_sha: org.opencontainers.image.revision
//   - git ref: org.opencontainers.image.ref.name, classified as a tag when it
//     is a valid semantic version, otherwise as a branch
func (s *tidbcloudsrvc) readKernelImageMeta(ctx context.Context, image string) kernelImageMeta {
	configBytes, err := crane.Config(image, crane.WithContext(ctx))
	if err != nil {
		s.Logger.Err(err).Str("image", image).Msg("read image config labels failed")
		return kernelImageMeta{}
	}

	var config v1.ConfigFile
	if err := json.Unmarshal(configBytes, &config); err != nil {
		s.Logger.Err(err).Str("image", image).Msg("unmarshal image config failed")
		return kernelImageMeta{}
	}
	if config.Config.Labels == nil {
		return kernelImageMeta{}
	}

	labels := config.Config.Labels
	meta := kernelImageMeta{
		Repo:      githubRepoFromSource(labels[ociLabelSource]),
		CommitSHA: strings.TrimSpace(labels[ociLabelRevision]),
	}
	meta.GitTag, meta.Branch = classifyKernelImageRef(labels[ociLabelRefName])
	s.Logger.Info().Any("labels", labels).Str("image", image).Msg("kernel image labels")
	return meta
}

// classifyKernelImageRef classifies the git ref stored in the image label as a
// tag when it is a valid semantic version, otherwise as a branch. branch and
// git_tag are mutually exclusive.
func classifyKernelImageRef(ref string) (gitTag, branch string) {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return "", ""
	}
	if semver.IsValid(ref) {
		return ref, ""
	}
	return "", ref
}
