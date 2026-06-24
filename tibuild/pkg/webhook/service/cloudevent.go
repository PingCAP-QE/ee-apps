package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"

	rest "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type CloudEventService interface {
	Handle(event cloudevents.Event)
}

type DevBuildCEServer struct {
	ds rest.DevBuildService
}

func NewDevBuildCEServer(ds rest.DevBuildService) DevBuildCEServer {
	return DevBuildCEServer{ds: ds}
}

func (s DevBuildCEServer) Handle(event cloudevents.Event) {
	if slog.Default().Enabled(context.Background(), slog.LevelInfo) {
		eventjson, _ := event.MarshalJSON()
		slog.Info("received event", "ev", string(eventjson))
	}
	pipeline, bid, err := eventToDevbuildTekton(event)
	if err != nil {
		slog.Error("parse tekton event failed", "error", err.Error())
		return
	}
	if bid == 0 {
		slog.Error("parse tekton event devbuild id failed")
		return
	}

	_, err = s.ds.MergeTektonStatus(context.TODO(), bid, *pipeline, rest.DevBuildSaveOption{})
	if err != nil {
		slog.Error("not devbuild event")
		return
	}
}

func eventToDevbuildTekton(event cloudevents.Event) (pipeline *rest.TektonPipeline, bid int, err error) {
	data := struct {
		PipelineRun tekton.PipelineRun `json:"pipelineRun"`
	}{}
	event.DataAs(&data)
	pipelinerun := data.PipelineRun
	CEContext := pipelinerun.Annotations["tekton.dev/ce-context"]
	source_event := struct {
		Source  string `json:"source"`
		Subject string `json:"subject"`
	}{}
	err = json.Unmarshal([]byte(CEContext), &source_event)
	if err != nil {
		return nil, 0, fmt.Errorf("unmarshal ce-context error:%w", err)
	}
	if strings.Contains(source_event.Source, "tibuild.pingcap.net/api/devbuild") {
		bids := source_event.Subject
		bid, err := strconv.Atoi(bids)
		if err != nil {
			return nil, 0, fmt.Errorf("decode devbuild id failed:%w", err)
		}
		pipeline, err := toDevbuildPipeline(pipelinerun)
		if err != nil {
			return nil, 0, err
		}
		switch ty := event.Context.GetType(); ty {
		case "dev.tekton.event.pipelinerun.started.v1":
			pipeline.Status = rest.BuildStatusProcessing
		case "dev.tekton.event.pipelinerun.successful.v1":
			pipeline.Status = rest.BuildStatusSuccess
		case "dev.tekton.event.pipelinerun.failed.v1":
			pipeline.Status = rest.BuildStatusFailure
		default:
			slog.Error("unknown tekton event type", "type", ty)
		}
		return pipeline, bid, nil
	} else {
		return nil, 0, fmt.Errorf("not devbuild event")
	}
}

func toDevbuildPipeline(pipeline tekton.PipelineRun) (*rest.TektonPipeline, error) {
	images, err := rest.ParseTektonImage(pipeline.Status.PipelineResults)
	if err != nil {
		return nil, fmt.Errorf("parse image failed:%w", err)
	}
	var startAt, endAt *time.Time
	if pipeline.Status.StartTime != nil {
		startAt = &pipeline.Status.StartTime.Time
	}
	if pipeline.Status.CompletionTime != nil {
		endAt = &pipeline.Status.CompletionTime.Time
	}
	return &rest.TektonPipeline{
		Name:         pipeline.Name,
		Platform:     rest.ParsePlatform(pipeline),
		GitHash:      rest.ParseGitHash(pipeline),
		OciArtifacts: rest.ConvertOciArtifacts(pipeline),
		Images:       images,
		StartAt:      startAt,
		EndAt:        endAt,
	}, nil
}

