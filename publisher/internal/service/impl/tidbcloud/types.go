package tidbcloud

type OpsConfig struct {
	Stages    map[string]OpsStageConfig `json:"stages" yaml:"stages"`
	TiBuildV2 TiBuildV2Config           `json:"tibuild_v2" yaml:"tibuild_v2"`
}

type OpsStageConfig struct {
	APIBaseURL string                  `json:"api_base_url" yaml:"api_base_url"`
	APIKey     string                  `json:"api_key" yaml:"api_key"`
	Components map[string]OpsComponent `json:"components" yaml:"components"`
}

type OpsComponent struct {
	BaseImage   string `json:"base_image" yaml:"base_image"`
	ClusterType string `json:"cluster_type" yaml:"cluster_type"`
	GitHubRepo  string `json:"github_repo" yaml:"github_repo"`
}

type TiBuildV2Config struct {
	APIBaseURL string `json:"api_base_url" yaml:"api_base_url"`
	User       string `json:"user" yaml:"user"`
	Password   string `json:"password" yaml:"password"`
}

type OpsUpdateComponentRequest struct {
	ClusterType string `json:"cluster_type"`
	Version     string `json:"version"`
	BaseImage   string `json:"base_image"`
	Tag         string `json:"tag"`
	Policy      string `json:"policy"`
	Author      string `json:"author,omitempty"`
	ReleaseID   string `json:"release_id,omitempty"`
	ChangeID    string `json:"change_id,omitempty"`
}

type OpsUpdateComponentResponse struct {
	InstanceID int `json:"instance_id"`
}

type OpsKernelImageSyncRequest struct {
	SourceApplicant string                           `json:"source_applicant"`
	SourceReleaseID string                           `json:"source_release_id,omitempty"`
	SourceChangeID  string                           `json:"source_change_id,omitempty"`
	Repo            string                           `json:"repo"`
	Branch          string                           `json:"branch,omitempty"`
	CommitSHA       string                           `json:"commit_sha"`
	GitTag          string                           `json:"git_tag,omitempty"`
	Images          []OpsKernelImageSyncRequestImage `json:"images"`
}

type OpsKernelImageSyncRequestImage struct {
	SourceRepository string `json:"source_repository"`
	SourceTag        string `json:"source_tag"`
}

type TiBuildTagMetadataResponse struct {
	Author string `json:"author"`
	Meta   struct {
		OpsReq struct {
			ReleaseID string `json:"release_id"`
			ChangeID  string `json:"change_id"`
		} `json:"ops_req"`
	} `json:"meta"`
}

// TestPlatformsConfig defines the configuration for the test platforms service.
type TestPlatformsConfig struct {
	TCMS TCMSConfig `json:"tcms" yaml:"tcms"`
}

// TCMSConfig defines the configuration for the TCMS API.
type TCMSConfig struct {
	APIBaseURL string `json:"api_base_url" yaml:"api_base_url"`
	AuthToken  string `json:"auth_token" yaml:"auth_token"`
}
