package impl

import (
	"context"
	"fmt"
	"net/http"
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
		return nil, &devbuild.HTTPError{Code: http.StatusBadRequest, Message: "github full repo not found"}
	}

	// 2. get the commit sha
	commitSha := getGhRefSha(ctx, s.ghClient, githubFullRepo, p.Request.GitRef)

	// 3. create the entity
	create := s.dbClient.DevBuild.Create().
		SetProduct(p.Request.Product).
		SetEdition(p.Request.Edition).
		SetVersion(p.Request.Version).
		SetGithubRepo(githubFullRepo).
		SetGitRef(p.Request.GitRef).
		SetGitSha(commitSha).
		SetNillableIsHotfix(p.Request.IsHotfix).
		SetCreatedAt(time.Now()).
		SetCreatedBy(p.CreatedBy).
		SetNillablePluginGitRef(p.Request.PluginGitRef).
		SetNillablePipelineEngine(p.Request.PipelineEngine).
		SetStatus("pending")

	// TODO: get the commit sha and set it in `create`.
	return create.Save(ctx)
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
