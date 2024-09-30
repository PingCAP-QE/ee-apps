package main

const (
	EventTypeTiupPublishRequest = "tiup-publish-request"
	FromTypeOci    = "oci"
	FromTypeHTTP   = "http"
)

type Config struct {
	Brokers     []string `yaml:"brokers"`
	Topic       string   `yaml:"topic"`
	Credentials struct {
		Type     string `yaml:"type"`
		Username string `yaml:"username"`
		Password string `yaml:"password"`
	} `yaml:"credentials"`
	ConsumerGroup string `yaml:"consumerGroup"`
	MirrorUrl     string `yaml:"mirrorUrl"`
}

type PublishRequestEvent struct {
	MirrorName string      `json:"mirror_name,omitempty"` // staging or production.
	From       From        `json:"from,omitempty"`
	Publish    PublishInfo `json:"publish,omitempty"`
}
type From struct {
	Type string    `json:"type,omitempty"`
	Oci  *FromOci  `json:"oci,omitempty"`
	HTTP *FromHTTP `json:"http,omitempty"`
}

type PublishInfo struct {
	Name        string `json:"name,omitempty"`
	OS          string `json:"os,omitempty"`
	Arch        string `json:"arch,omitempty"`
	Version     string `json:"version,omitempty"`
	Description string `json:"description,omitempty"`
	EntryPoint  string `json:"entry_point,omitempty"`
	Standalone  bool   `json:"standalone,omitempty"`
}

type FromOci struct {
	Repo string `json:"repo,omitempty"`
	Tag  string `json:"tag,omitempty"`
	File string `json:"file,omitempty"`
}

type FromHTTP struct {
	URL string `json:"url,omitempty"`
}

// 2024-09-23T20:02:29.932583969+08:00 tiup mirror publish ctl v8.4.0-alpha-nightly ctl-v8.4.0-alpha-41-gedb43c053-darwin-amd64.tar.gz ctl --os darwin --arch amd64 --desc "TiDB controller suite"

type TiupMirror struct {
	Name string `yaml:"name,omitempty" json:"name,omitempty"`
	URL  string `yaml:"url,omitempty" json:"url,omitempty"`
	Auth struct {
		Username string `yaml:"username,omitempty" json:"username,omitempty"`
		Password string `yaml:"password,omitempty" json:"password,omitempty"`
	} `yaml:"auth,omitempty" json:"auth,omitempty"`
}
