package config

type Service struct {
	Store   Store   `yaml:"store" json:"store"`
	Github  Github  `yaml:"github" json:"github"`
	Jenkins Jenkins `yaml:"jenkins" json:"jenkins"`
	Tekton  Tekton  `yaml:"tekton" json:"tekton"`
	// StaticDir string `yaml:"static_dir,omitempty" json:"static_dir,omitempty"`
}

type Github struct {
	Token string `yaml:"token,omitempty" json:"token,omitempty"`
}

type Store struct {
	Driver string `yaml:"driver,omitempty" json:"driver,omitempty"`
	DSN    string `yaml:"dsn,omitempty" json:"dsn,omitempty"`
}

type Jenkins struct {
	UserName string `yaml:"username,omitempty" json:"username,omitempty"`
	PassWord string `yaml:"password,omitempty" json:"password,omitempty"`
}

type Tekton struct {
	CloudeventEndpoint string `yaml:"cloudevent_endpoint,omitempty" json:"cloudevent_endpoint,omitempty"`
	ViewURL            string `yaml:"view_url,omitempty" json:"view_url,omitempty"`
	OciFileDownloadURL string `yaml:"oci_file_download_url,omitempty" json:"oci_file_download_url,omitempty"`
}

type RestApiSecret struct {
	AdminToken   string `yaml:"admintoken,omitempty" json:"admintoken,omitempty"`
	TiBuildToken string `yaml:"tibuildtoken,omitempty" json:"tibuildtoken,omitempty"`
}
