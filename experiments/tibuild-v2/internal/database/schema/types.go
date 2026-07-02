package schema

import "time"

// TektonStatus represents the status of Tekton pipelines for a devbuild.
// This mirrors the Goa-generated devbuild.TektonStatus struct.
type TektonStatus struct {
	Pipelines        []TektonPipeline `json:"pipelines,omitempty"`
	TriggersEventIds []string         `json:"triggersEventIDs,omitempty"`
}

// TektonPipeline represents a single Tekton pipeline run.
// This mirrors the Goa-generated devbuild.TektonPipeline struct.
type TektonPipeline struct {
	Name         string          `json:"name"`
	Namespace    string          `json:"namespace"`
	Status       string          `json:"status"`
	StartAt      *time.Time      `json:"startAt,omitzero"`
	EndAt        *time.Time      `json:"endAt,omitzero"`
	Images       []ImageArtifact `json:"images,omitzero"`
	OciArtifacts []OciArtifact   `json:"ociArtifacts,omitzero"`
	Platform     string          `json:"platform,omitzero"`
	URL          string          `json:"url,omitzero"`
}

// ImageArtifact represents a container image artifact.
type ImageArtifact struct {
	Platform string `json:"platform,omitzero"`
	URL      string `json:"url,omitzero"`
}

// OciArtifact represents an OCI artifact.
type OciArtifact struct {
	Files []string `json:"files,omitzero"`
	Repo  string   `json:"repo,omitzero"`
	Tag   string   `json:"tag,omitzero"`
}
