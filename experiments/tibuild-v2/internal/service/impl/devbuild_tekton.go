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

	// Platforms
	LinuxAmd64  = "linux/amd64"
	LinuxArm64  = "linux/arm64"
	DarwinAmd64 = "darwin/amd64"
	DarwinArm64 = "darwin/arm64"
)

func (s *devbuildsrvc) triggerTknBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// 1. Compose a cloud event from the record.
	events, err := newDevBuildCloudEvents(record)
	if err != nil {
		s.logger.Err(err).Msg("failed to create cloud events")
		return nil, err
	}

	// 2. Send the cloud events to tekton listener that serves for tibuild.
	eventsCount := len(events)
	for i, event := range events {
		respEvent, result := s.tektonCloudEventClient.Request(ctx, *event)
		if !protocol.IsACK(result) {
			s.logger.Err(result).Msgf("failed to send cloud event(index %d/%d", i, eventsCount)
			return nil, fmt.Errorf("failed to send cloud event: %w", result)
		}

		// debug the resp event
		if respEvent != nil {
			s.logger.Debug().Stringer("response_event", respEvent).Msgf("event(index %d/%d) sent ok with response", i, eventsCount)
		}
	}

	return record, nil
}

func newDevBuildCloudEvents(record *ent.DevBuild) ([]*cloudevents.Event, error) {
	events := []*cloudevents.Event{}
	for _, platform := range parsePlatforms(record.Platform) {
		event, err := newDevBuildCloudEvent(record, platform)
		if err != nil {
			return nil, err
		}
		events = append(events, event)
	}

	return events, nil
}

func newDevBuildCloudEvent(record *ent.DevBuild, platform string) (*cloudevents.Event, error) {
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
		return nil, fmt.Errorf("unknown git ref format")
	}

	event.SetData(cloudevents.ApplicationJSON, eventData)
	event.SetSubject(fmt.Sprint(record.ID))
	event.SetSource("tibuild.pingcap.net/api/devbuilds/" + fmt.Sprint(record.ID))
	event.SetExtension("user", record.CreatedBy)
	event.SetExtension("paramProfile", normalizeEdition(string(record.Edition)))
	event.SetExtension("paramIsHotfix", record.IsHotfix)
	event.SetExtension("paramIsPushGcr", record.IsPushGcr)
	if record.GithubRepo != "" {
		event.SetExtension("paramGithubRepo", record.GithubRepo)
	}
	if record.BuilderImg != "" {
		event.SetExtension("paramBuilderImage", record.BuilderImg)
	}
	if platform != "" {
		event.SetExtension("paramPlatform", platform)
	}

	return &event, nil
}

func parsePlatforms(platformExp string) []string {
	switch platformExp {
	case LinuxAmd64, LinuxArm64, DarwinAmd64, DarwinArm64:
		return []string{platformExp}
	case "linux", "linux/all":
		return []string{LinuxAmd64, LinuxArm64}
	case "darwin", "darwin/all":
		return []string{DarwinAmd64, DarwinArm64}
	case "", "all":
		return []string{""}
	default:
		return strings.Split(platformExp, ",")
	}
}
