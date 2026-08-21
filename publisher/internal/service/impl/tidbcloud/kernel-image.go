package tidbcloud

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/go-resty/resty/v2"
	"github.com/google/go-containerregistry/pkg/crane"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"golang.org/x/mod/semver"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
)

const (
	syncKernelImageApiURL = "/devops/kernel-images/build-callback"
)

// kernelImageMeta holds source code metadata read from the source image labels.
type kernelImageMeta struct {
	Repo      string
	Branch    string
	GitTag    string
	CommitSHA string
}

// kernelImageMetaReader reads source code metadata from an image. The default
// reads the image OCI labels; it is overridable in tests.
type kernelImageMetaReader func(ctx context.Context, image string) kernelImageMeta

// RequestSyncKernelImage implements request-sync-kernel-image.
func (s *tidbcloudsrvc) RequestSyncKernelImage(ctx context.Context, p *tidbcloud.RequestSyncKernelImagePayload) (res string, err error) {
	if err := validateKernelImageSyncPayload(p); err != nil {
		s.Logger.Error().Err(err).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}
	if s.opsCfg == nil {
		err = fmt.Errorf("tidbcloud ops config is not configured")
		s.Logger.Error().Err(err).Str("stage", p.Stage).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}

	readMeta := s.kernelImageMetaReader
	if readMeta == nil {
		readMeta = s.readKernelImageMeta
	}

	payload, err := s.buildKernelImageSyncRequest(ctx, p.Images, readMeta)
	if err != nil {
		return "", err
	}

	client := s.opsRestyClient(p.Stage)
	if client == nil {
		err = fmt.Errorf("ops client is nil for stage %q: stage not found in config", p.Stage)
		s.Logger.Error().Err(err).Str("stage", p.Stage).Msg("requestSyncKernelImage failed")
		return "", err
	}

	return s.callKernelImageSyncAPI(ctx, p.Stage, client, payload)
}

func validateKernelImageSyncPayload(p *tidbcloud.RequestSyncKernelImagePayload) error {
	if p == nil {
		return fmt.Errorf("payload is nil")
	}
	if strings.TrimSpace(p.Stage) == "" {
		return fmt.Errorf("stage is empty")
	}
	if len(p.Images) == 0 {
		return fmt.Errorf("images is empty")
	}
	return nil
}

// buildKernelImageSyncRequest collects the per-image source metadata, verifies
// that all images were built from the same repo commit, resolves the tibuild
// tag metadata once, and assembles the upstream ops request.
func (s *tidbcloudsrvc) buildKernelImageSyncRequest(ctx context.Context, images []string, readMeta kernelImageMetaReader) (*OpsKernelImageSyncRequest, error) {
	sources := make([]OpsKernelImageSyncRequestImage, 0, len(images))
	var repo, branch, gitTag, commitSHA, firstSourceTag string
	for i, image := range images {
		src, meta, err := s.kernelImageSource(ctx, image, readMeta)
		if err != nil {
			return nil, err
		}
		if i == 0 {
			repo, branch, gitTag, commitSHA, firstSourceTag = meta.Repo, meta.Branch, meta.GitTag, meta.CommitSHA, src.SourceTag
		} else if meta.Repo != repo || meta.CommitSHA != commitSHA || meta.GitTag != gitTag || meta.Branch != branch {
			err = fmt.Errorf("source images are not built from the same repo commit")
			s.Logger.Error().Err(err).
				Str("source_image", image).Str("repo", meta.Repo).Str("branch", meta.Branch).Str("git_tag", meta.GitTag).Str("commit_sha", meta.CommitSHA).
				Str("first_image", images[0]).Str("first_repo", repo).Str("first_branch", branch).Str("first_git_tag", gitTag).Str("first_commit_sha", commitSHA).
				Msg("tidbcloud.request-sync-kernel-image failed: source image metadata mismatch")
			return nil, err
		}
		sources = append(sources, src)
	}

	applicant, releaseID, changeID := s.resolveKernelImageApplicant(ctx, repo, firstSourceTag)

	return &OpsKernelImageSyncRequest{
		SourceApplicant: applicant,
		SourceReleaseID: releaseID,
		SourceChangeID:  changeID,
		Repo:            repo,
		Branch:          branch,
		CommitSHA:       commitSHA,
		GitTag:          gitTag,
		Images:          sources,
	}, nil
}

// kernelImageSource parses the image reference and reads its source code
// metadata from the image OCI labels.
func (s *tidbcloudsrvc) kernelImageSource(ctx context.Context, image string, readMeta kernelImageMetaReader) (OpsKernelImageSyncRequestImage, kernelImageMeta, error) {
	sourceRepository, sourceTag, err := parseImageRepoTag(image)
	if err != nil {
		s.Logger.Error().Err(err).Str("source_image", image).Msg("tidbcloud.request-sync-kernel-image failed to parse source image")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}

	meta := readMeta(ctx, image)
	if meta.Repo == "" || meta.CommitSHA == "" {
		err = fmt.Errorf("failed to read source image metadata: missing repo or commit_sha labels")
		s.Logger.Error().Err(err).Str("source_image", image).Str("repo", meta.Repo).Str("commit_sha", meta.CommitSHA).Msg("tidbcloud.request-sync-kernel-image failed")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}
	if meta.GitTag == "" && meta.Branch == "" {
		err = fmt.Errorf("failed to read source image metadata: missing git ref label")
		s.Logger.Error().Err(err).Str("source_image", image).Msg("tidbcloud.request-sync-kernel-image failed")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}

	return OpsKernelImageSyncRequestImage{SourceRepository: sourceRepository, SourceTag: sourceTag}, meta, nil
}

// resolveKernelImageApplicant resolves the applicant and ops request IDs from
// the tibuild tag metadata. Only real tidbx git tags (vX.Y.Z, vX.Y.Z-nextgen or
// legacy vX.Y.Z-nextgen.YYYYMM.N) carry this metadata; other image tags (e.g.
// <branch>-<commit> daily builds) have no git tag to query, so the tibuild-v2
// call is skipped. The fields are optional: when the metadata is unavailable
// they are left empty instead of failing the request. Since all images share
// the same repo commit, the metadata is resolved once from the first image's
// source tag.
func (s *tidbcloudsrvc) resolveKernelImageApplicant(ctx context.Context, repo, sourceTag string) (applicant, releaseID, changeID string) {
	if !isSupportedKernelImageTag(sourceTag) {
		s.Logger.Info().Str("repo", repo).Str("tag", sourceTag).Msg("skip tibuild tag metadata query: source tag is not a supported tidbx git tag")
		return "", "", ""
	}
	md, err := s.getTiBuildTagMetadata(ctx, repo, sourceTag)
	if err != nil {
		s.Logger.Warn().Err(err).Str("repo", repo).Str("tag", sourceTag).Msg("failed to get tibuild tag metadata for kernel image")
		return "", "", ""
	}
	if md == nil {
		return "", "", ""
	}
	return md.Author, md.Meta.OpsReq.ReleaseID, md.Meta.OpsReq.ChangeID
}

// callKernelImageSyncAPI posts the kernel image sync request to the ops
// platform build callback and returns the response body.
func (s *tidbcloudsrvc) callKernelImageSyncAPI(ctx context.Context, stage string, client *resty.Client, payload *OpsKernelImageSyncRequest) (string, error) {
	s.Logger.Debug().
		Str("stage", stage).
		Int("images", len(payload.Images)).
		Str("repo", payload.Repo).
		Str("branch", payload.Branch).
		Str("git_tag", payload.GitTag).
		Msg("calling ops platform kernel image build callback")

	resp, err := client.R().
		SetContext(ctx).
		SetBody(payload).
		Post(syncKernelImageApiURL)
	if err != nil {
		err = fmt.Errorf("call ops kernel image build callback: %w", err)
		s.Logger.Error().
			Err(err).Int("status", resp.StatusCode()).
			Str("response_body", string(resp.Body())).
			Str("stage", stage).
			Msg("requestSyncKernelImage request failed")
		return "", err
	}
	if resp.IsError() {
		err = fmt.Errorf("call ops kernel image build callback: http status %d: %s", resp.StatusCode(), strings.TrimSpace(string(resp.Body())))
		s.Logger.Error().
			Err(err).
			Int("status", resp.StatusCode()).
			Str("response_body", string(resp.Body())).
			Str("stage", stage).
			Msg("requestSyncKernelImage returned error status")
		return "", err
	}

	s.Logger.Info().
		Str("stage", stage).
		Int("images", len(payload.Images)).
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

var supportedKernelImageTagRegexp = regexp.MustCompile(`^v\d+\.\d+\.\d+(-nextgen(\.\d{6}\.\d+)?)?$`)

// isSupportedKernelImageTag reports whether the image tag can be resolved to a
// real tidbx git tag (vX.Y.Z, vX.Y.Z-nextgen or legacy vX.Y.Z-nextgen.YYYYMM.N),
// and thus whether tibuild-v2 may hold tag metadata for it.
func isSupportedKernelImageTag(tag string) bool {
	return supportedKernelImageTagRegexp.MatchString(tag)
}
