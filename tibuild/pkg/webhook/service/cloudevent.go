package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"

	rest "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	"gopkg.in/yaml.v3"
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
	pipeline, bid, err := eventToDevbuildTekton(event)
	if err != nil {
		log.Printf("parse tekton event failed: %s", err.Error())
		return
	}
	if bid == 0 {
		log.Printf("parse tekton event build id failed")
		return
	}
	_, err = s.ds.MergeTektonStatus(context.TODO(), bid, *pipeline, rest.DevBuildSaveOption{})
	if err != nil {
		log.Print("not devbuild event")
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
		pipeline, err := to_devbuild_pipeline(&pipelinerun)
		if err != nil {
			return nil, 0, err
		}
		switch event.Context.GetType() {
		case "dev.tekton.event.pipelinerun.started.v1":
			pipeline.Status = rest.BuildStatusProcessing
		case "dev.tekton.event.pipelinerun.successful.v1":
			pipeline.Status = rest.BuildStatusSuccess
		case "dev.tekton.event.pipelinerun.failed.v1":
			pipeline.Status = rest.BuildStatusFailure
		default:
			log.Print("unknown tekton event type")
		}
		return pipeline, bid, nil
	} else {
		return nil, 0, fmt.Errorf("not devbuild event")
	}
}

func to_devbuild_pipeline(pipeline *tekton.PipelineRun) (*rest.TektonPipeline, error) {
	images, err := parse_tekton_image(pipeline.Status.PipelineResults)
	if err != nil {
		return nil, fmt.Errorf("parse image failed:%w", err)
	}
	return &rest.TektonPipeline{
		Name:          pipeline.Name,
		Platform:      parse_platform(pipeline),
		GitHash:       parse_git_hash(pipeline),
		OrasArtifacts: parse_oras_files(pipeline),
		Images:        images,
	}, nil
}

func parse_platform(pipeline *tekton.PipelineRun) rest.Platform {
	os := ""
	arch := ""
	for _, p := range pipeline.Spec.Params {
		switch p.Name {
		case "os":
			os = p.Value.StringVal
		case "arch":
			arch = p.Value.StringVal
		}
	}
	if os != "" && arch != "" {
		return rest.Platform(os + "/" + arch)
	} else {
		return ""
	}
}

func parse_git_hash(pipeline *tekton.PipelineRun) string {
	for _, p := range pipeline.Spec.Params {
		if p.Name == "git-revision" {
			v := p.Value.StringVal
			if len(v) == 40 {
				return v
			}
		}
	}
	return ""
}

func parse_oras_files(pipeline *tekton.PipelineRun) []rest.OrasArtifact {
	var rt []rest.OrasArtifact
	for _, r := range pipeline.Status.PipelineResults {
		if r.Name == "pushed-binaries" {
			v, err := parse_oras_file(r.Value.StringVal)
			if err != nil {
				log.Printf("can not parse oras file %s", err.Error())
			}
			rt = append(rt, *v)
		}
	}
	return rt
}

type TektonOrasStruct struct {
	OCI struct {
		Repo   string `json:"repo"`
		Tag    string `json:"tag"`
		Digest string `json:"digest"`
	} `json:"oci"`
	Files []string `json:"files"`
}

func parse_oras_file(text string) (*rest.OrasArtifact, error) {
	tekton_oras := TektonOrasStruct{}
	err := yaml.Unmarshal([]byte(text), &tekton_oras)
	if err != nil {
		return nil, err
	}
	return &rest.OrasArtifact{
		Repo:  tekton_oras.OCI.Repo,
		Tag:   tekton_oras.OCI.Tag,
		Files: tekton_oras.Files,
	}, nil
}

type TektonImageStruct struct {
	Images []struct {
		URL string `json:"url"`
	} `json:"images"`
}

func parse_tekton_image(results []tekton.PipelineRunResult) ([]rest.ImageArtifact, error) {
	var rt []rest.ImageArtifact
	for _, r := range results {
		if r.Name == "pushed-images" {
			images := TektonImageStruct{}
			err := yaml.Unmarshal([]byte(r.Value.StringVal), &images)
			if err != nil {
				return nil, err
			}
			for _, image := range images.Images {
				rt = append(rt, rest.ImageArtifact{URL: image.URL})
			}
		}
	}
	return rt, nil
}
