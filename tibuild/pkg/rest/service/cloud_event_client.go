package service

import (
	"context"
	"fmt"
	"log/slog"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"
	"github.com/google/go-github/v61/github"
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
	event, err := NewDevBuildCloudEvent(dev)
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

func NewDevBuildCloudEvent(dev DevBuild) (*cloudevents.Event, error) {
	event := cloudevents.NewEvent()
	event.SetType(devbuild_ce_type)
	event.SetSubject(fmt.Sprint(dev.ID))
	event.SetExtension("user", dev.Meta.CreatedBy)
	event.SetSource("tibuild.pingcap.net/api/devbuilds/" + fmt.Sprint(dev.ID))
	repo := GHRepoToStruct(dev.Spec.GithubRepo)

	if ref := GitRefToGHRef(dev.Spec.GitRef); ref != "" {
		eventData := &github.PushEvent{
			Ref:    github.String(ref),
			After:  github.String(dev.Spec.GitHash),
			Before: github.String("00000000000000000000000000000000000000000"),
			Repo: &github.PushEventRepository{
				Name:     &repo.Repo,
				CloneURL: github.String(repo.URL()),
				Owner: &github.User{
					Login: &repo.Owner,
				},
			},
		}
		event.SetData(cloudevents.ApplicationJSON, eventData)
		return &event, nil
	} else {
		return nil, fmt.Errorf("unkown git ref format")
	}
}

const devbuild_ce_type = "net.pingcap.tibuild.devbuild.push"
