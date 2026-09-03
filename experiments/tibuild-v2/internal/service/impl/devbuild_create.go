package impl

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

const (
	tektonEngine  = "tekton"
	jenkinsEngine = "jenkins"
)

func (s *devbuildsrvc) newBuildEntity(ctx context.Context, p *devbuild.CreatePayload) (*ent.DevBuild, error) {
	// NoNeed: guess enterprise plugin ref.
	// fill for fips
	// set for default pipeline engine.

	// 1. get the github full repo by product.
	githubFullRepo := s.productRepoMap[p.Request.Product]
	if githubFullRepo == "" {
		return nil, &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "unknown product"}
	}
	if !validGitRef(p.Request.GitRef) {
		return nil, &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: "gitRef must use branch/<name>, tag/<name>, pull/<number>, commit/<40-char SHA>, or a raw 40-char SHA"}
	}

	// 2. get the commit sha
	_, commitSha := getGhRefAndSha(ctx, s.ghClient, githubFullRepo, p.Request.GitRef)

	// Normalize raw hex SHA to "commit/<sha>" for consistent storage.
	gitRef := p.Request.GitRef
	if isHex(gitRef) {
		gitRef = "commit/" + gitRef
	}

	// 3. create the entity
	edition := normalizeEdition(p.Request.Edition)
	create := s.dbClient.DevBuild.Create().
		SetProduct(p.Request.Product).
		SetEdition(edition).
		SetVersion(derefString(p.Request.Version)).
		SetGithubRepo(githubFullRepo).
		SetGitRef(gitRef).
		SetGitHash(commitSha).
		SetNillableIsHotfix(p.Request.IsHotfix).
		SetCreatedAt(time.Now()).
		SetCreatedBy(derefString(p.CreatedBy)).
		SetNillablePluginGitRef(p.Request.PluginGitRef).
		SetNillablePipelineEngine(p.Request.PipelineEngine).
		SetPlatform(p.Request.Platform).
		SetStatus("PENDING")

	return create.Save(ctx)
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

func (s *devbuildsrvc) triggerBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	switch record.PipelineEngine {
	case tektonEngine:
		return s.triggerTknBuild(ctx, record)
	case jenkinsEngine:
		return s.triggerJenkinsBuild(ctx, record)
	default:
		return nil, fmt.Errorf("unsupported pipeline engine: %s", record.PipelineEngine)
	}
}
