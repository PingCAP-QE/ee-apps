package main

const (
	EventTypeTiupPublishRequest = "tiup-publish-request"

	FromTypeOci  = "oci"
	FromTypeHTTP = "http"
)

type Config struct {
	Brokers     []string `yaml:"brokers" json:"brokers,omitempty"`
	Topic       string   `yaml:"topic" json:"topic,omitempty"`
	Credentials struct {
		Type     string `yaml:"type" json:"type,omitempty"`
		Username string `yaml:"username" json:"username,omitempty"`
		Password string `yaml:"password" json:"password,omitempty"`
	} `yaml:"credentials" json:"credentials,omitempty"`
	ConsumerGroup  string `yaml:"consumer_group" json:"consumer_group,omitempty"`
	MirrorUrl      string `yaml:"mirror_url" json:"mirror_url,omitempty"`
	LarkWebhookURL string `yaml:"lark_webhook_url" json:"lark_webhook_url,omitempty"`
}

type PublishRequestEvent struct {
	From    From        `json:"from,omitempty"`
	Publish PublishInfo `json:"publish,omitempty"`
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
