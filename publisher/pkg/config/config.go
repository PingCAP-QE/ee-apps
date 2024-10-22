package config

type Worker struct {
	Kafka struct {
		KafkaBasic    `yaml:",inline" json:",inline"`
		ConsumerGroup string `yaml:"consumer_group" json:"consumer_group,omitempty"`
	} `yaml:"kafka" json:"kafka,omitempty"`
	Redis          Redis  `yaml:"redis" json:"redis,omitempty"`
	MirrorUrl      string `yaml:"mirror_url" json:"mirror_url,omitempty"`
	LarkWebhookURL string `yaml:"lark_webhook_url" json:"lark_webhook_url,omitempty"`
}

type Service struct {
	Kafka       KafkaBasic `yaml:"kafka" json:"kafka,omitempty"`
	Redis       Redis      `yaml:"redis" json:"redis,omitempty"`
	EventSource string     `yaml:"event_source" json:"event_source,omitempty"`
}

type Redis struct {
	Addr     string `yaml:"addr" json:"addr,omitempty"`
	DB       int    `yaml:"db" json:"db,omitempty"`
	Username string `yaml:"username" json:"username,omitempty"`
	Password string `yaml:"password" json:"password,omitempty"`
}

type KafkaBasic struct {
	Brokers     []string `yaml:"brokers" json:"brokers,omitempty"`
	Topic       string   `yaml:"topic" json:"topic,omitempty"`
	Credentials struct {
		Type     string `yaml:"type" json:"type,omitempty"`
		Username string `yaml:"username" json:"username,omitempty"`
		Password string `yaml:"password" json:"password,omitempty"`
	} `yaml:"credentials" json:"credentials,omitempty"`
}
