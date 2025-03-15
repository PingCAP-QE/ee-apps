package impl

import (
	"context"
	"errors"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

func (s *devbuildsrvc) newBuildEntity(ctx context.Context, p *devbuild.CreatePayload) (*ent.DevBuild, error) {
	// NoNeed: guess enterprise plugin ref.
	// fill for fips
	// set for default pipeline engine.

	// 1. get the github full repo by product.
	githubFullRepo := s.productRepoMap[p.Request.Product]
	if githubFullRepo == "" {
		return nil, errors.New("github full repo not found")
	}

	// 2. get the commit sha
	commitSha := getGhRefSha(ctx, s.ghClient, githubFullRepo, p.Request.GitRef)

	// 3. create the entity
	create := s.dbClient.DevBuild.Create().
		SetProduct(string(p.Request.Product)).
		SetGithubRepo(githubFullRepo).
		SetGitRef(p.Request.GitRef).
		SetGitSha(commitSha).
		SetEdition(string(p.Request.Edition)).
		SetNillableIsHotfix(p.Request.IsHotfix).
		SetCreatedAt(time.Now()).
		SetCreatedBy(p.CreatedBy).
		SetNillablePluginGitRef(p.Request.PluginGitRef).
		SetNillablePipelineEngine((*string)(p.Request.PipelineEngine)).
		SetStatus("pending")

	// TODO: get the commit sha and set it in `create`.
	return create.Save(ctx)
}

func (s *devbuildsrvc) triggerBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// TODO: trigger the actual build process according to the record.
	return nil, nil
}
