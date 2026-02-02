package tidbcloud

type Payload struct {
	ClusterType string `json:"cluster_type"`
	Version     string `json:"version"`
	BaseImage   string `json:"base_image"`
	Tag         string `json:"tag"`
	Policy      string `json:"policy"`
}
