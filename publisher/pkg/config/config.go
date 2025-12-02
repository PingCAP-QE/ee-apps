package config

// Workers represents the configuration for workers.
type Workers struct {
	Tiup       *Worker `yaml:"tiup,omitempty" json:"tiup,omitempty"`
	FileServer *Worker `yaml:"file_server,omitempty" json:"file_server,omitempty"`
}

// Worker represents the configuration for a worker.
type Worker struct {
	Kafka struct {
		KafkaBasic    `yaml:",inline" json:",inline"`
		ConsumerGroup string `yaml:"consumer_group" json:"consumer_group,omitempty"`
	} `yaml:"kafka,omitempty" json:"kafka,omitzero"`
	Redis   Redis             `yaml:"redis" json:"redis"`
	Options map[string]string `yaml:"options,omitempty" json:"options,omitempty"`
}

// Service represents the configuration for a service.
type Service struct {
	Kafka       KafkaBasic `yaml:"kafka" json:"kafka"`
	Redis       Redis      `yaml:"redis" json:"redis"`
	EventSource string     `yaml:"event_source" json:"event_source,omitempty"`

	// service config for special service.
	// <service-name>: <service-config>
	Services map[string]any `yaml:"services,omitempty" json:"services,omitempty"`
}

// Redis represents the configuration to connect to a Redis instance.
type Redis struct {
	Addr     string `yaml:"addr" json:"addr,omitempty"`
	DB       int    `yaml:"db" json:"db,omitempty"`
	Username string `yaml:"username" json:"username,omitempty"`
	Password string `yaml:"password" json:"password,omitempty"`
}

// KafkaBasic represents the basic configuration for a Kafka instance.
type KafkaBasic struct {
	Brokers     []string `yaml:"brokers" json:"brokers,omitempty"`
	Topic       string   `yaml:"topic" json:"topic,omitempty"`
	Credentials struct {
		Type     string `yaml:"type" json:"type,omitempty"`
		Username string `yaml:"username" json:"username,omitempty"`
		Password string `yaml:"password" json:"password,omitempty"`
	} `yaml:"credentials" json:"credentials"`
}
