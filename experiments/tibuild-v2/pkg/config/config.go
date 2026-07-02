package config

type Service struct {
	Store   Store   `yaml:"store" json:"store"`
	Github  Github  `yaml:"github" json:"github"`
	Jenkins Jenkins `yaml:"jenkins" json:"jenkins"`
	Tekton  Tekton  `yaml:"tekton" json:"tekton"`
	Lark    Lark    `yaml:"lark" json:"lark"`

	// ProductRepoMap is a map of product names to their respective Github full repository names(<org>/<repo>).
	ProductRepoMap map[string]string `yaml:"product_repo_map" json:"product_repo_map"`
	// ImageMirrorURLMap is a map prefixes for transformation between direct url to mirror url.
	ImageMirrorURLMap map[string]string `yaml:"image_mirror_url_map" json:"image_mirror_url_map"`
}

type Github struct {
	Token string `yaml:"token,omitempty" json:"token,omitempty"`
}

type Store struct {
	Driver string `yaml:"driver,omitempty" json:"driver,omitempty"`
	DSN    string `yaml:"dsn,omitempty" json:"dsn,omitempty"`
}

type Jenkins struct {
	URL      string `yaml:"url,omitempty" json:"url,omitempty"`
	Username string `yaml:"username,omitempty" json:"username,omitempty"`
	Password string `yaml:"password,omitempty" json:"password,omitempty"`
	JobName  string `yaml:"job_name,omitempty" json:"job_name,omitempty"`
}

type Tekton struct {
	CloudeventEndpoint string `yaml:"cloudevent_endpoint,omitempty" json:"cloudevent_endpoint,omitempty"`
	ViewURL            string `yaml:"view_url,omitempty" json:"view_url,omitempty"`
	OciFileDownloadURL string `yaml:"oci_file_download_url,omitempty" json:"oci_file_download_url,omitempty"`
	ReconcilerInterval string `yaml:"reconciler_interval,omitempty" json:"reconciler_interval,omitempty"`
	ReconcilerSince    string `yaml:"reconciler_since,omitempty" json:"reconciler_since,omitempty"`
}

type Lark struct {
	Enabled   bool   `yaml:"enabled" json:"enabled"`
	AppID     string `yaml:"app_id,omitempty" json:"app_id,omitempty"`
	AppSecret string `yaml:"app_secret,omitempty" json:"app_secret,omitempty"`
}
