package impl

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"

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
		return nil, &devbuild.HTTPError{Code: http.StatusBadRequest, Message: "github full repo not found"}
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

func (s *devbuildsrvc) triggerTknBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// 1. Compose a cloud event from the record.
	event, err := newDevBuildCloudEvent(record)
	if err != nil {
		s.logger.Err(err).Msg("failed to create cloud event")
		return nil, err
	}

	// 2. Send the cloud event to tekton listener that serves for tibuild.
	if result := s.tektonCloudEventClient.Send(ctx, *event); !protocol.IsACK(result) {
		s.logger.Err(result).Msg("failed to send cloud event")
		return nil, fmt.Errorf("failed to send cloud event: %w", result)
	}

	return nil, nil
}

func newDevBuildCloudEvent(record *ent.DevBuild) (*cloudevents.Event, error) {
	event := cloudevents.NewEvent()

	var eventData any
	switch {
	case strings.HasPrefix(record.GitRef, "branch/"):
		ref := strings.Replace(record.GitRef, "branch/", "refs/heads/", 1)
		event.SetType(ceTypeFakeGHPushDevBuild)
		eventData = newFakeGitHubPushEventPayload(record.GithubRepo, ref, record.GitSha)
	case strings.HasPrefix(record.GitRef, "tag/"):
		ref := strings.Replace(record.GitRef, "tag/", "refs/tags/", 1)
		event.SetType(ceTypeFakeGHCreateDevBuild)
		eventData = newFakeGitHubTagCreateEventPayload(record.GithubRepo, ref)
	case strings.HasPrefix(record.GitRef, "pull/"):
		event.SetType(ceTypeFakeGHPRDevBuild)
		prNumberStr := strings.TrimPrefix(record.GitRef, "pull/")
		prNumber, err := strconv.Atoi(prNumberStr)
		if err != nil {
			return nil, fmt.Errorf("invalid PR number: %s", prNumberStr)
		}
		eventData = newFakeGitHubPullRequestPayload(record.GithubRepo, record.GitRef,
			record.GitSha, prNumber)
	default:
		return nil, fmt.Errorf("unkown git ref format")
	}

	event.SetData(cloudevents.ApplicationJSON, eventData)
	event.SetSubject(fmt.Sprint(record.ID))
	event.SetSource("tibuild.pingcap.net/api/devbuilds/" + fmt.Sprint(record.ID))
	event.SetExtension("user", record.CreatedBy)
	event.SetExtension("paramProfile", string(record.Edition))
	if record.BuilderImg != "" {
		event.SetExtension("paramBuilderImage", record.BuilderImg)
	}

	return &event, nil
}
