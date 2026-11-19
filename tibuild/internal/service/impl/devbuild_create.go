package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

const (
	tektonEngine  = "tekton"
	jenkinsEngine = "jenkins"
)

func (s *devbuildsrvc) newBuildEntity(ctx context.Context, p *devbuild.CreatePayload) (*ent.DevBuild, gitRefInfo, error) {
	if err := validateProductRequest(p.Request); err != nil {
		return nil, gitRefInfo{}, err
	}
	// One snapshot per request: the allowlists and the product map must come
	// from the same configuration revision.
	policy := s.currentPolicy()
	if err := validateBuilderImage(policy.builderImageAllowlist, derefString(p.Request.BuilderImg)); err != nil {
		if isAllowlistDenied(err) {
			s.recordBuilderImageDenied("create", derefString(p.Request.BuilderImg), p.Request.Product, derefString(p.CreatedBy))
		}
		return nil, gitRefInfo{}, err
	}

	// NoNeed: guess enterprise plugin ref.
	// fill for fips
	// set for default pipeline engine.

	// 1. get the github full repo. Honor an explicit repo from the request,
	// and fall back to the product default only when it is not provided.
	githubFullRepo, err := resolveGithubRepo(derefString(p.Request.GithubRepo), policy.productRepoMap, p.Request.Product, policy.repoAllowlist)
	if err != nil {
		if isAllowlistDenied(err) {
			s.recordRepoDenied("create", derefString(p.Request.GithubRepo), p.Request.Product, derefString(p.CreatedBy))
		}
		return nil, gitRefInfo{}, err
	}
	if !validGitRef(p.Request.GitRef) {
		return nil, gitRefInfo{}, &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "gitRef must use branch/<name>, tag/<name>, pull/<number>, commit/<40-char SHA>, or a raw 40-char SHA"}
	}

	// 2. resolve the ref against GitHub before persisting anything. A missing
	// ref or an upstream failure must not leave a DevBuild row behind.
	refInfo, err := getGitRefInfo(ctx, s.ghClient, githubFullRepo, p.Request.GitRef)
	if err != nil {
		return nil, gitRefInfo{}, s.refResolutionError(err, githubFullRepo, p.Request.GitRef)
	}

	// Normalize raw hex SHA to "commit/<sha>" for consistent storage.
	gitRef := p.Request.GitRef
	if isHex(gitRef) {
		gitRef = "commit/" + gitRef
	}

	// 3. create the entity
	edition := normalizeEdition(p.Request.Edition)
	if capability, ok := productBuildCapabilities[p.Request.Product]; ok && edition == "" {
		edition = capability.Editions[0]
	}
	create := s.dbClient.DevBuild.Create().
		SetProduct(p.Request.Product).
		SetEdition(edition).
		SetVersion(derefString(p.Request.Version)).
		SetGithubRepo(githubFullRepo).
		SetGitRef(gitRef).
		SetGitHash(refInfo.SHA).
		SetNillableIsHotfix(p.Request.IsHotfix).
		SetCreatedAt(time.Now()).
		SetCreatedBy(derefString(p.CreatedBy)).
		SetNillablePluginGitRef(p.Request.PluginGitRef).
		SetNillablePipelineEngine(p.Request.PipelineEngine).
		SetPlatform(p.Request.Platform).
		SetNillableBuilderImg(p.Request.BuilderImg).
		SetStatus("PENDING")

	record, err := create.Save(ctx)
	if err != nil {
		return nil, gitRefInfo{}, err
	}
	return record, refInfo, nil
}

// refResolutionError maps a ref resolution failure to the public API error:
// 400 when GitHub says the ref does not exist, 500 for upstream failures.
func (s *devbuildsrvc) refResolutionError(err error, repo, ref string) error {
	var notFound *gitRefNotFoundError
	if errors.As(err, &notFound) {
		return &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: notFound.Error()}
	}

	s.logger.Err(err).Str("github_repo", repo).Str("git_ref", ref).Msg("failed to resolve devbuild git ref")
	return &devbuild.DevBuildInternalServerError{Code: http.StatusInternalServerError, Message: err.Error()}
}

// gitRefInfoFromRecord rebuilds the resolved ref from persisted fields without
// calling GitHub. Pull request base refs are not persisted; the PR trigger
// binding derives its refspec from the PR number, so BaseRef stays empty.
func gitRefInfoFromRecord(record *ent.DevBuild) gitRefInfo {
	info := gitRefInfo{SHA: record.GitHash}
	switch {
	case strings.HasPrefix(record.GitRef, "branch/"):
		info.Ref = "refs/heads/" + strings.TrimPrefix(record.GitRef, "branch/")
	case strings.HasPrefix(record.GitRef, "tag/"):
		info.Ref = "refs/tags/" + strings.TrimPrefix(record.GitRef, "tag/")
	case strings.HasPrefix(record.GitRef, "commit/"):
		info.Ref = record.GitHash
	case isHex(record.GitRef):
		info.Ref = record.GitRef
	}
	return info
}

// resolveGithubRepo returns the repository a build should use. An explicit
// repo from the request wins; otherwise the product default is used. It
// returns a bad request error when neither is available or the repo is not in
// the <owner>/<repo> form, and a forbidden error when an explicit repo is
// outside the allowlist.
func resolveGithubRepo(explicitRepo string, productRepoMap map[string]string, product string, allow config.RepoAllowlist) (string, error) {
	githubFullRepo := explicitRepo
	explicit := explicitRepo != ""
	if githubFullRepo == "" {
		githubFullRepo = productRepoMap[product]
	}
	if githubFullRepo == "" {
		return "", &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "unknown product or githubRepo"}
	}
	if parts := strings.SplitN(githubFullRepo, "/", 3); len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "githubRepo must be in the form <owner>/<repo>"}
	}
	// Only an explicit value is judged: the product default is trusted by the
	// configuration invariant that requires a non-empty allowlist to cover it.
	if explicit {
		if err := checkExplicitRepo(githubFullRepo, productRepoMap, product, allow); err != nil {
			return "", err
		}
	}
	return githubFullRepo, nil
}

func validGitRef(ref string) bool {
	if isHex(ref) {
		return true
	}
	for _, prefix := range []string{"branch/", "tag/"} {
		if strings.HasPrefix(ref, prefix) {
			return strings.TrimSpace(strings.TrimPrefix(ref, prefix)) != ""
		}
	}
	if strings.HasPrefix(ref, "commit/") {
		return isHex(strings.TrimPrefix(ref, "commit/"))
	}
	if strings.HasPrefix(ref, "pull/") {
		number, err := strconv.Atoi(strings.TrimPrefix(ref, "pull/"))
		return err == nil && number > 0
	}
	return false
}

func (s *devbuildsrvc) triggerBuild(ctx context.Context, record *ent.DevBuild, refInfo gitRefInfo) (*ent.DevBuild, error) {
	switch record.PipelineEngine {
	case tektonEngine:
		return s.triggerTknBuild(ctx, record, refInfo)
	case jenkinsEngine:
		return s.triggerJenkinsBuild(ctx, record)
	default:
		return nil, fmt.Errorf("unsupported pipeline engine: %s", record.PipelineEngine)
	}
}
