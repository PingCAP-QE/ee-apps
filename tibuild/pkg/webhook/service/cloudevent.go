package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"

	rest "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
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
	pipelinerun := tekton.PipelineRun{}
	event.DataAs(&pipelinerun)
	CEContext := pipelinerun.Annotations["tekton.dev/ce-context"]
	ev := cloudevents.NewEvent()
	err := json.Unmarshal([]byte(CEContext), &ev)
	if err != nil {
		log.Printf("unmarshal ce-context error")
		return
	}
	ce_cource := ev.Context.GetType()
	if ce_cource == "net.pingcap.tibuild.devbuild.push" {
		bids := ev.Context.GetSubject()
		bid, err := strconv.Atoi(bids)
		if err != nil {
			log.Printf("decode devbuild id failed")
			return
		}
		pipeline := to_devbuild_pipeline(&pipelinerun)
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
		_, err = s.ds.MergeTektonStatus(context.TODO(), bid, pipeline, rest.DevBuildSaveOption{})
		if err != nil {
			log.Printf("merge tekton pipeline failed: %s", err.Error())
		}
	} else {
		log.Print("not devbuild event")
		return
	}
}

func to_devbuild_pipeline(pipeline *tekton.PipelineRun) rest.TektonPipeline {
	images, err := parse_tekton_image(pipeline.Status.Results)
	if err != nil {
		log.Printf("parse image failed:%s", err.Error())
	}
	return rest.TektonPipeline{
		Name:      pipeline.Name,
		Platform:  parse_platform(pipeline),
		GitHash:   parse_git_hash(pipeline),
		OrasFiles: parse_oras_files(pipeline),
		Images:    images,
	}
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

func parse_oras_files(pipeline *tekton.PipelineRun) []rest.OrasFile {
	var rt []rest.OrasFile
	for _, r := range pipeline.Status.Results {
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

func parse_oras_file(text string) (*rest.OrasFile, error) {
	tekton_oras := TektonOrasStruct{}
	err := yaml.Unmarshal([]byte(text), &tekton_oras)
	if err != nil {
		return nil, err
	}
	return &rest.OrasFile{
		URL:   fmt.Sprintf("%s:%s", tekton_oras.OCI.Repo, tekton_oras.OCI.Tag),
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
