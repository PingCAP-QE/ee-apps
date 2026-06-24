package service

import (
	"fmt"
	"log/slog"

	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	"gopkg.in/yaml.v3"
)

// ParsePlatform extracts the platform (os/arch) from PipelineRun params.
func ParsePlatform(pipeline tekton.PipelineRun) string {
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
		return os + "/" + arch
	}
	return ""
}

// ParseGitHash extracts the git revision from PipelineRun params.
func ParseGitHash(pipeline tekton.PipelineRun) string {
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

// ConvertOciArtifacts extracts OCI artifacts from PipelineRun results.
func ConvertOciArtifacts(pipeline tekton.PipelineRun) []OciArtifact {
	var rt []OciArtifact
	for _, r := range pipeline.Status.PipelineResults {
		if r.Name == "pushed-binaries" {
			v, err := convertOciArtifact(r.Value.StringVal)
			if err != nil {
				slog.Error("can not parse oci file", "error", err.Error())
				// this make error can be seen by frontend, and not block other result
				v = &OciArtifact{Repo: "parse_error", Tag: r.Value.StringVal, Files: []string{"error_parse_artifact"}}
			}
			rt = append(rt, *v)
		}
	}
	return rt
}

type TektonOciArtifactStruct struct {
	OCI struct {
		Repo   string `json:"repo"`
		Tag    string `json:"tag"`
		Digest string `json:"digest"`
	} `json:"oci"`
	Files []string `json:"files"`
}

func convertOciArtifact(text string) (*OciArtifact, error) {
	tekton_oci_artifact := TektonOciArtifactStruct{}
	err := yaml.Unmarshal([]byte(text), &tekton_oci_artifact)
	if err != nil {
		return nil, err
	}
	return &OciArtifact{
		Repo:  tekton_oci_artifact.OCI.Repo,
		Tag:   tekton_oci_artifact.OCI.Tag,
		Files: tekton_oci_artifact.Files,
	}, nil
}

// TektonImageStruct represents the image structure in Tekton results.
type TektonImageStruct struct {
	Images []struct {
		Repo          string   `json:"repo" yaml:"repo"`
		Platform      string   `json:"platform" yaml:"platform"`
		Tag           string   `json:"tag" yaml:"tag"`
		Tags          []string `json:"tags" yaml:"tags"`
		MultiArchTags []string `json:"multi_arch_tags" yaml:"multi_arch_tags"`
		URL           string   `json:"url" yaml:"url"`
	} `json:"images" yaml:"images"`
}

// ParseTektonImage extracts images from PipelineRun results.
func ParseTektonImage(results []tekton.PipelineRunResult) ([]ImageArtifact, error) {
	var rt []ImageArtifact
	for _, r := range results {
		if r.Name == "pushed-images" {
			images := TektonImageStruct{}
			err := yaml.Unmarshal([]byte(r.Value.StringVal), &images)
			if err != nil {
				return nil, err
			}
			for _, image := range images.Images {
				imgURL := fmt.Sprintf("%s:%s", image.Repo, image.Tag)
				rt = append(rt, ImageArtifact{URL: imgURL, Platform: image.Platform})

				// if it has multi arch tags, then we append the multi-arch image with the first tag.
				if len(image.MultiArchTags) > 0 {
					multiArchImgURL := fmt.Sprintf("%s:%s", image.Repo, image.MultiArchTags[0])
					rt = append(rt, ImageArtifact{URL: multiArchImgURL, Platform: MultiArch})
				}
			}
		}
	}
	return rt, nil
}
