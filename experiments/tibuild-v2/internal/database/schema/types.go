package schema

// TektonStatus represents the status of Tekton pipelines for a devbuild.
// This mirrors the Goa-generated devbuild.TektonStatus struct.
type TektonStatus struct {
	Pipelines        []TektonPipeline `json:"pipelines,omitempty"`
	TriggersEventIds []string         `json:"triggers_event_ids,omitempty"`
}

// TektonPipeline represents a single Tekton pipeline run.
// This mirrors the Goa-generated devbuild.TektonPipeline struct.
type TektonPipeline struct {
	Name         string         `json:"name"`
	Status       string         `json:"status"`
	StartAt      string         `json:"start_at,omitempty"`
	EndAt        string         `json:"end_at,omitempty"`
	GitSha       string         `json:"git_sha,omitempty"`
	Images       []ImageArtifact `json:"images,omitempty"`
	OciArtifacts []OciArtifact  `json:"oci_artifacts,omitempty"`
	Platform     string         `json:"platform,omitempty"`
	URL          string         `json:"url,omitempty"`
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
