package service

import (
	"context"
	"fmt"
	"log"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/github"
)

type BuildTrigger interface {
	TriggerDevBuild(ctx context.Context, dev DevBuild) error
}

func NewCEClient(endpoint string) CloudEventClient {
	client, err := cloudevents.NewClientHTTP()
	if err != nil {
		log.Fatalf("create cloudevent http client failed %s", err.Error())
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
	if result := s.client.Send(c, *event); result != nil {
		log.Printf("failed to send, %v", result)
		return fmt.Errorf("failed to send ce:%w", result)
	}
	return nil
}

func NewDevBuildCloudEvent(dev DevBuild) (*cloudevents.Event, error) {
	event := cloudevents.NewEvent()
	event.SetType(devbuild_ce_type)
	event.SetSubject(fmt.Sprint(dev.ID))
	event.SetExtension("user", dev.Meta.CreatedBy)
	event.SetSource("https://tibuild.pingcap.net/api/devbuilds/" + fmt.Sprint(dev.ID))

	if branchName, found := strings.CutPrefix(dev.Spec.GitRef, "branch/"); found {
		eventData := &github.PushEvent{
			Ref:   github.String(fmt.Sprintf("refs/heads/%s", branchName)),
			After: github.String(branchName),
			Repo: &github.PushEventRepository{
				Name:     github.String(string(dev.Spec.Product)),
				CloneURL: github.String(fmt.Sprintf("https://github.com/%s", dev.Spec.GithubRepo)),
				Owner: &github.PushEventRepoOwner{
					Name: github.String("pingcap"),
				},
			},
		}
		event.SetData(cloudevents.ApplicationJSON, eventData)
		return &event, nil
	} else {
		return nil, fmt.Errorf("not a branch name")
	}
}

const devbuild_ce_type = "net.pingcap.tibuild.devbuild.push"
