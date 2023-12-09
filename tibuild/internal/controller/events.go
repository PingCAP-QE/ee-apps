package controller

import (
	"context"
	"fmt"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v57/github"
)

func (pt *PipelineTriggerStruct) NewCloudEvent(buildID int) *cloudevents.Event {
	/*
		ArtifactType string `form:"artifact_type" json:"artifact_type" validate:"required"`
		Branch       string `form:"branch" json:"branch" validate:"required"`
		Component    string `form:"component" json:"component" validate:"required"`
		PipelineId   int    `form:"pipeline_id" json:"pipeline_id" validate:"required,numeric"`
		Version      string `form:"version" json:"version" validate:"required,startswith=v"`
		TriggeredBy  string `form:"triggered_by" json:"triggered_by" validate:"required"`
		PushGCR      string `form:"push_gcr" json:"push_gcr" validate:"required"`
	*/
	event := cloudevents.NewEvent()
	// TODO: fill them
	event.SetSubject(fmt.Sprint(buildID))
	event.SetExtension("user", pt.TriggeredBy)
	event.SetSource("https://tibuild.pingcap/com/dev-build/")
	eventData := &github.PushEvent{
		Ref:   github.String(fmt.Sprintf("refs/heads/%s", pt.Branch)),
		After: github.String(pt.Branch),
		Repo: &github.PushEventRepository{
			Name:     &pt.Component,
			CloneURL: github.String(fmt.Sprintf("https://github.com/tikv/%s", pt.Component)),
			Owner: &github.User{
				Login: github.String("pingcap"),
			},
		},
	}
	event.SetData(cloudevents.ApplicationJSON, eventData)

	return &event
}

func sendEventsForDevBuild(sinkURL string, event cloudevents.Event) cloudevents.Result {
	client, _ := cloudevents.NewClientHTTP(cloudevents.WithTarget(sinkURL))
	return client.Send(context.Background(), event)
}
