package config

type Service struct {
	Store   Store   `yaml:"store" json:"store"`
	Github  Github  `yaml:"github" json:"github"`
	Jenkins Jenkins `yaml:"jenkins" json:"jenkins"`
	Tekton  Tekton  `yaml:"tekton" json:"tekton"`
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
}
