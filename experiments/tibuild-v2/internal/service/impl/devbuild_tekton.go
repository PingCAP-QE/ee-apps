package impl

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

const (
	ceTypeFakeGHPushDevBuild   = "net.pingcap.tibuild.devbuild.push"
	ceTypeFakeGHPRDevBuild     = "net.pingcap.tibuild.devbuild.pull_request"
	ceTypeFakeGHCreateDevBuild = "net.pingcap.tibuild.devbuild.create"
)

func (s *devbuildsrvc) triggerTknBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// 1. Compose a cloud event from the record.
	event, err := newDevBuildCloudEvent(record)
	if err != nil {
		s.logger.Err(err).Msg("failed to create cloud event")
		return nil, err
	}

	// 2. Send the cloud event to tekton listener that serves for tibuild.
	respEvent, result := s.tektonCloudEventClient.Request(ctx, *event)
	if !protocol.IsACK(result) {
		s.logger.Err(result).Msg("failed to send cloud event")
		return nil, fmt.Errorf("failed to send cloud event: %w", result)
	}

	// debug the resp event
	if respEvent != nil {
		s.logger.Debug().Stringer("response_event", respEvent).Msg("event sent ok with response")
	}

	return record, nil
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
	if record.GithubRepo != "" {
		event.SetExtension("paramGithubRepo", record.GithubRepo)
	}
	if record.BuilderImg != "" {
		event.SetExtension("paramBuilderImage", record.BuilderImg)
	}

	return &event, nil
}
