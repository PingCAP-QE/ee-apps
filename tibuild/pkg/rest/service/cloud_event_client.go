package service

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"
	"github.com/google/go-github/v61/github"
)

const (
	ceTypeFakeGHPushDevBuild   = "net.pingcap.tibuild.devbuild.push"
	ceTypeFakeGHPRDevBuild     = "net.pingcap.tibuild.devbuild.pull_request"
	ceTypeFakeGHCreateDevBuild = "net.pingcap.tibuild.devbuild.create"
)

type BuildTrigger interface {
	TriggerDevBuild(ctx context.Context, dev DevBuild) error
}

func NewCEClient(endpoint string) CloudEventClient {
	client, err := cloudevents.NewClientHTTP()
	if err != nil {
		slog.Error("create cloudevent http client failed", "reason", err)
	}
	return CloudEventClient{
		client:   client,
		endpoint: endpoint,
	}
}

type CloudEventClient struct {
	client   cloudevents.Client
	endpoint string
}

func (s CloudEventClient) TriggerDevBuild(ctx context.Context, dev DevBuild) error {
	event, err := newDevBuildCloudEvent(dev)
	if err != nil {
		return err
	}

	c := cloudevents.ContextWithTarget(ctx, s.endpoint)
	if result := s.client.Send(c, *event); !protocol.IsACK(result) {
		slog.ErrorContext(ctx, "failed to send", "reason", result)
		return fmt.Errorf("failed to send ce:%w", result)
	}

	return nil
}

func newDevBuildCloudEvent(dev DevBuild) (*cloudevents.Event, error) {
	repo := GHRepoToStruct(dev.Spec.GithubRepo)

	var eventType string
	var eventData any
	switch {
	case strings.HasPrefix(dev.Spec.GitRef, "branch/"):
		ref := strings.Replace(dev.Spec.GitRef, "branch/", "refs/heads/", 1)
		eventType = ceTypeFakeGHPushDevBuild
		eventData = newFakeGitHubPushEventPayload(repo.Owner, repo.Repo, ref, dev.Spec.GitHash)
	case strings.HasPrefix(dev.Spec.GitRef, "tag/"):
		ref := strings.Replace(dev.Spec.GitRef, "tag/", "refs/tags/", 1)
		eventType = ceTypeFakeGHCreateDevBuild
		eventData = newFakeGitHubTagCreateEventPayload(repo.Owner, repo.Repo, ref)
	case strings.HasPrefix(dev.Spec.GitRef, "pull/"):
		eventType = ceTypeFakeGHPRDevBuild
		eventData = newFakeGitHubPullRequestPayload(repo.Owner, repo.Repo, dev.Spec.prBaseRef,
			dev.Spec.GitHash, dev.Spec.prNumber)
	default:
		return nil, fmt.Errorf("unkown git ref format")
	}

	event := cloudevents.NewEvent()
	event.SetType(eventType)
	event.SetData(cloudevents.ApplicationJSON, eventData)
	event.SetSubject(fmt.Sprint(dev.ID))
	event.SetSource("tibuild.pingcap.net/api/devbuilds/" + fmt.Sprint(dev.ID))
	event.SetExtension("user", dev.Meta.CreatedBy)
	event.SetExtension("paramProfile", string(dev.Spec.Edition))
	if dev.Spec.BuilderImg != "" {
		event.SetExtension("paramBuilderImage", dev.Spec.BuilderImg)
	}
	if dev.Spec.Platform != "" {
		event.SetExtension("paramPlatform", dev.Spec.Platform)
	}

	return &event, nil
}

func newFakeGitHubPushEventPayload(owner, repo, ref, sha string) *github.PushEvent {
	return &github.PushEvent{
		Ref:    github.String(ref),
		After:  github.String(sha),
		Before: github.String("00000000000000000000000000000000000000000"),
		Repo: &github.PushEventRepository{
			FullName: github.String(fmt.Sprintf("%s/%s", owner, repo)),
			Name:     github.String(repo),
			CloneURL: github.String(fmt.Sprintf("https://github.com/%s/%s.git", owner, repo)),
			Owner: &github.User{
				Login: github.String(owner),
			},
		},
	}
}

func newFakeGitHubTagCreateEventPayload(owner, repo, ref string) *github.CreateEvent {
	return &github.CreateEvent{
		Ref:     github.String(strings.Replace(ref, "refs/tags/", "", 1)),
		RefType: github.String("tag"),
		Repo: &github.Repository{
			FullName: github.String(fmt.Sprintf("%s/%s", owner, repo)),
			Name:     github.String(repo),
			CloneURL: github.String(fmt.Sprintf("https://github.com/%s/%s.git", owner, repo)),
			Owner: &github.User{
				Login: github.String(owner),
			},
		},
	}
}

func newFakeGitHubPullRequestPayload(owner, repo, baseRef, headSHA string, number int) *github.PullRequestEvent {
	return &github.PullRequestEvent{
		Action: github.String("opened"),
		Number: github.Int(number),
		PullRequest: &github.PullRequest{
			Number: github.Int(number),
			State:  github.String("open"),
			Head: &github.PullRequestBranch{
				SHA: github.String(headSHA),
			},
			Base: &github.PullRequestBranch{
				Ref: github.String(baseRef),
			},
		},
		Repo: &github.Repository{
			FullName: github.String(fmt.Sprintf("%s/%s", owner, repo)),
			Name:     github.String(repo),
			CloneURL: github.String(fmt.Sprintf("https://github.com/%s/%s.git", owner, repo)),
			Owner: &github.User{
				Login: github.String(owner),
			},
		},
	}
}
