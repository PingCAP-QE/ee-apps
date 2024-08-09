package kafka

type Producer struct {
	TopicMapping map[string]string `yaml:"topic_mapping,omitempty" json:"topic_mapping,omitempty"` // event type to topic.
	DefaultTopic string            `yaml:"default_topic,omitempty" json:"default_topic,omitempty"`
}

type Consumer struct {
	GroupID      string            `yaml:"group_id,omitempty" json:"group_id,omitempty"`
	TopicMapping map[string]string `yaml:"topic_mapping,omitempty" json:"topic_mapping,omitempty"` // event type to topic.
}
