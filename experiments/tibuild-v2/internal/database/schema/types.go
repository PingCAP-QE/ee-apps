package schema

import "time"

// TektonStatus represents the status of Tekton pipelines for a devbuild.
// This mirrors the Goa-generated devbuild.TektonStatus struct.
type TektonStatus struct {
	Pipelines        []TektonPipeline `json:"pipelines,omitempty"`
	TriggersEventIds []string         `json:"triggers_event_ids,omitempty"`
}

// TektonPipeline represents a single Tekton pipeline run.
// This mirrors the Goa-generated devbuild.TektonPipeline struct.
type TektonPipeline struct {
	Name         string          `json:"name"`
	Namespace    string          `json:"namespace"`
	Status       string          `json:"status"`
	StartAt      *time.Time      `json:"start_at,omitempty"`
	EndAt        *time.Time      `json:"end_at,omitempty"`
	Images       []ImageArtifact `json:"images,omitempty"`
	OciArtifacts []OciArtifact   `json:"oci_artifacts,omitempty"`
	Platform     string          `json:"platform,omitempty"`
	URL          string          `json:"url,omitempty"`
}

// ImageArtifact represents a container image artifact.
type ImageArtifact struct {
	Platform string `json:"platform,omitempty"`
	URL      string `json:"url,omitempty"`
}

// OciArtifact represents an OCI artifact.
type OciArtifact struct {
	Files []string `json:"files"`
	Repo  string   `json:"repo"`
	Tag   string   `json:"tag"`
}
