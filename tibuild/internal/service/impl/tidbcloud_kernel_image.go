package impl

import (
	"context"
	"fmt"
	"regexp"
	"strings"

	"golang.org/x/mod/semver"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

const syncKernelImageAPIPath = "/devops/kernel-images/build-callback"

// kernelImageMeta holds source code metadata read from the source image labels.
type kernelImageMeta struct {
	Repo      string
	Branch    string
	GitTag    string
	CommitSHA string
}

// kernelImageMetaReader reads source code metadata from an image. The default
// reads the image OCI labels; tests inject a stub.
type kernelImageMetaReader func(ctx context.Context, image string) kernelImageMeta

// RequestSyncKernelImage asks the ops platform to sync kernel images built
// from the same repo commit.
func (s *tidbcloudsrvc) RequestSyncKernelImage(ctx context.Context, p *tidbcloud.RequestSyncKernelImagePayload) (string, error) {
	if err := validateKernelImageSyncPayload(p); err != nil {
		s.logger.Error().Err(err).Msg("tidbcloud.request-sync-kernel-image failed")
		return "", err
	}

	opsCfg := s.opsConfig()
	if opsCfg == nil {
		return "", fmt.Errorf("tidbcloud ops config is not configured")
	}
	stageCfg, ok := opsCfg.Stages[p.Stage]
	if !ok || stageCfg.APIBaseURL == "" {
		return "", fmt.Errorf("ops client is nil for stage %q: stage not found in config", p.Stage)
	}

	readMeta := s.kernelImageMetaReader
	if readMeta == nil {
		readMeta = s.readKernelImageMeta
	}

	payload, err := s.buildKernelImageSyncRequest(ctx, p.Images, readMeta)
	if err != nil {
		return "", err
	}

	headers := map[string]string{}
	if stageCfg.APIKey != "" {
		headers["x-api-key"] = stageCfg.APIKey
	}

	status, body, err := s.postJSON(ctx, strings.TrimSuffix(stageCfg.APIBaseURL, "/")+syncKernelImageAPIPath, headers, payload)
	if err != nil {
		return "", fmt.Errorf("call ops kernel image build callback: %w", err)
	}
	if status < 200 || status >= 300 {
		return "", fmt.Errorf("call ops kernel image build callback: http status %d: %s", status, strings.TrimSpace(string(body)))
	}

	s.logger.Info().Str("stage", p.Stage).Int("images", len(payload.Images)).
		Str("repo", payload.Repo).Str("branch", payload.Branch).Str("git_tag", payload.GitTag).
		Msg("requestSyncKernelImage succeeded")
	return strings.TrimSpace(string(body)), nil
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

// buildKernelImageSyncRequest collects per-image source metadata, verifies that
// all images were built from the same repo commit, resolves the tag metadata
// once, and assembles the ops request.
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
			s.logger.Error().Err(err).
				Str("source_image", image).Str("repo", meta.Repo).Str("branch", meta.Branch).
				Str("git_tag", meta.GitTag).Str("commit_sha", meta.CommitSHA).
				Str("first_image", images[0]).Str("first_repo", repo).Str("first_branch", branch).
				Str("first_git_tag", gitTag).Str("first_commit_sha", commitSHA).
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

func (s *tidbcloudsrvc) kernelImageSource(ctx context.Context, image string, readMeta kernelImageMetaReader) (OpsKernelImageSyncRequestImage, kernelImageMeta, error) {
	sourceRepository, sourceTag, err := parseImageRepoTag(image)
	if err != nil {
		s.logger.Error().Err(err).Str("source_image", image).
			Msg("tidbcloud.request-sync-kernel-image failed to parse source image")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}

	meta := readMeta(ctx, image)
	if meta.Repo == "" || meta.CommitSHA == "" {
		err = fmt.Errorf("failed to read source image metadata: missing repo or commit_sha labels")
		s.logger.Error().Err(err).Str("source_image", image).Str("repo", meta.Repo).Str("commit_sha", meta.CommitSHA).
			Msg("tidbcloud.request-sync-kernel-image failed")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}
	if meta.GitTag == "" && meta.Branch == "" {
		err = fmt.Errorf("failed to read source image metadata: missing git ref label")
		s.logger.Error().Err(err).Str("source_image", image).
			Msg("tidbcloud.request-sync-kernel-image failed")
		return OpsKernelImageSyncRequestImage{}, kernelImageMeta{}, err
	}

	return OpsKernelImageSyncRequestImage{SourceRepository: sourceRepository, SourceTag: sourceTag}, meta, nil
}

// resolveKernelImageApplicant resolves the applicant and ops request ids from
// the tag metadata. Only real tidbx git tags carry this metadata; daily builds
// have no tag to query, so the lookup is skipped and the fields stay empty.
func (s *tidbcloudsrvc) resolveKernelImageApplicant(ctx context.Context, repo, sourceTag string) (applicant, releaseID, changeID string) {
	if !isSupportedKernelImageTag(sourceTag) {
		s.logger.Info().Str("repo", repo).Str("tag", sourceTag).
			Msg("skip tibuild tag metadata query: source tag is not a supported tidbx git tag")
		return "", "", ""
	}
	md, err := s.getTiBuildTagMetadata(ctx, repo, sourceTag)
	if err != nil {
		s.logger.Warn().Err(err).Str("repo", repo).Str("tag", sourceTag).
			Msg("failed to get tibuild tag metadata for kernel image")
		return "", "", ""
	}
	if md == nil {
		return "", "", ""
	}
	return md.Author, md.ReleaseID, md.ChangeID
}

// readKernelImageMeta reads repo, commit SHA and git ref from the labels:
//   - repo: org.opencontainers.image.source
//   - commit_sha: org.opencontainers.image.revision
//   - git ref: org.opencontainers.image.ref.name, a tag when it is a valid
//     semantic version, otherwise a branch
func (s *tidbcloudsrvc) readKernelImageMeta(ctx context.Context, image string) kernelImageMeta {
	labels := s.readImageLabels(ctx, image)
	if labels == nil {
		return kernelImageMeta{}
	}

	meta := kernelImageMeta{
		Repo:      githubRepoFromSource(labels[ociLabelSource]),
		CommitSHA: strings.TrimSpace(labels[ociLabelRevision]),
	}
	meta.GitTag, meta.Branch = classifyKernelImageRef(labels[ociLabelRefName])
	return meta
}

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

func isSupportedKernelImageTag(tag string) bool {
	return supportedKernelImageTagRegexp.MatchString(tag)
}
