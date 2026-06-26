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
	ceTypeFakeGHPushDevBuild      = "net.pingcap.tibuild.devbuild.push"
	ceTypeFakeGHPrDevBuild        = "net.pingcap.tibuild.devbuild.pull_request"
	ceTypeFakeGHCreateDevBuild    = "net.pingcap.tibuild.devbuild.create"
	ceTypeFakeGHPushHotfixBuild   = "net.pingcap.tibuild.hotfix.push"
	ceTypeFakeGHPrHotfixBuild     = "net.pingcap.tibuild.hotfix.pull_request"
	ceTypeFakeGHCreateHotfixBuild = "net.pingcap.tibuild.hotfix.create"

	// Platforms
	LinuxAmd64  = "linux/amd64"
	LinuxArm64  = "linux/arm64"
	DarwinAmd64 = "darwin/amd64"
	DarwinArm64 = "darwin/arm64"
)

func (s *devbuildsrvc) triggerTknBuild(ctx context.Context, record *ent.DevBuild) (*ent.DevBuild, error) {
	// 1. Compose a cloud event from the record.
	events, err := s.newDevBuildCloudEvents(record)
	if err != nil {
		s.logger.Err(err).Msg("failed to create cloud events")
		return nil, err
	}

	// 2. Send the cloud events to tekton listener that serves for tibuild.
	eventsCount := len(events)
	var eventIDs []string
	for i, event := range events {
		respEvent, result := s.tektonCloudEventClient.Request(ctx, *event)
		if !protocol.IsACK(result) {
			s.logger.Err(result).Msgf("failed to send cloud event(index %d/%d", i, eventsCount)
			return nil, fmt.Errorf("failed to send cloud event: %w", result)
		}

		// Capture the eventID from the response
		if respEvent != nil {
			eventID := respEvent.Context.GetID()
			if eventID != "" {
				eventIDs = append(eventIDs, eventID)
				s.logger.Debug().Str("eventID", eventID).Msgf("event(index %d/%d) sent ok with eventID", i, eventsCount)
			} else {
				s.logger.Debug().Stringer("response_event", respEvent).Msgf("event(index %d/%d) sent ok with response but no eventID", i, eventsCount)
			}
		}
	}

	// 3. Store the eventIDs in TektonStatus
	if len(eventIDs) > 0 {
		tektonStatus := record.TektonStatus
		tektonStatus.TriggersEventIds = eventIDs
		record, err = s.dbClient.DevBuild.UpdateOneID(record.ID).
			SetTektonStatus(tektonStatus).
			Save(ctx)
		if err != nil {
			s.logger.Err(err).Msg("failed to store eventIDs in tekton_status")
			// Non-fatal: the build was triggered successfully, just the eventID storage failed
		}
	}

	return record, nil
}

func (s *devbuildsrvc) newDevBuildCloudEvents(record *ent.DevBuild) ([]*cloudevents.Event, error) {
	events := []*cloudevents.Event{}
	for _, platform := range parsePlatforms(record.Platform) {
		event, err := s.newDevBuildCloudEvent(record, platform)
		if err != nil {
			return nil, err
		}
		events = append(events, event)
	}

	return events, nil
}

func (s *devbuildsrvc) newDevBuildCloudEvent(record *ent.DevBuild, platform string) (*cloudevents.Event, error) {
	ref, sha := getGhRefAndSha(context.TODO(), s.ghClient, record.GithubRepo, record.GitRef)
	event := cloudevents.NewEvent()
	var eventData any
	switch {
	case strings.HasPrefix(record.GitRef, "branch/"):
		if record.IsHotfix {
			event.SetType(ceTypeFakeGHPushHotfixBuild)
		} else {
			event.SetType(ceTypeFakeGHPushDevBuild)
		}

		eventData = newFakeGitHubPushEventPayload(record.GithubRepo, ref, sha)
	case strings.HasPrefix(record.GitRef, "tag/"):
		if record.IsHotfix {
			event.SetType(ceTypeFakeGHCreateHotfixBuild)
		} else {
			event.SetType(ceTypeFakeGHCreateDevBuild)
		}

		eventData = newFakeGitHubTagCreateEventPayload(record.GithubRepo, ref)
	case strings.HasPrefix(record.GitRef, "pull/"):
		if record.IsHotfix {
			event.SetType(ceTypeFakeGHPrHotfixBuild)
		} else {
			event.SetType(ceTypeFakeGHPrDevBuild)
		}
		prNumberStr := strings.TrimPrefix(record.GitRef, "pull/")
		prNumber, err := strconv.Atoi(prNumberStr)
		if err != nil {
			return nil, fmt.Errorf("invalid PR number: %s", prNumberStr)
		}

		eventData = newFakeGitHubPullRequestPayload(record.GithubRepo, ref, sha, prNumber)
	default:
		return nil, fmt.Errorf("unknown git ref format")
	}

	event.SetData(cloudevents.ApplicationJSON, eventData)
	event.SetSubject(fmt.Sprint(record.ID))
	event.SetSource("tibuild.pingcap.net/api/v2/devbuilds/" + fmt.Sprint(record.ID))
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
	if record.PluginGitRef != "" {
		event.SetExtension("paramPluginGitRef", record.PluginGitRef)
	}

	return &event, nil
}

func parsePlatforms(platformExp string) []string {
	switch platformExp {
	case LinuxAmd64, LinuxArm64, DarwinAmd64, DarwinArm64:
		return []string{platformExp}
	case "linux", "linux/all", "":
		return []string{LinuxAmd64, LinuxArm64}
	case "darwin", "darwin/all":
		return []string{DarwinAmd64, DarwinArm64}
	case "all":
		return []string{""}
	default:
		return strings.Split(platformExp, ",")
	}
}
