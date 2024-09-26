package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"

	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/kafka"
)

type Store struct {
	Driver string `yaml:"driver,omitempty" json:"driver,omitempty"`
	DSN    string `yaml:"dsn,omitempty" json:"dsn,omitempty"`
}

type LarkBotApp struct {
	AppID     string `yaml:"app_id,omitempty" json:"app_id,omitempty"`
	AppSecret string `yaml:"app_secret,omitempty" json:"app_secret,omitempty"`
}

// TektonNotification notify config for tekton run events, the `Type` and `Subject` is "AND" logic.
type TektonNotification struct {
	// EventType of the cloud event, if you want it send all types, set value as "*".
	EventType string `yaml:"event_type,omitempty" json:"event_type,omitempty"`
	// EventSubjectReg match regex of the cloud event, if you want it send for all
	//	subjects, set the value as "" or ".*".
	EventSubjectReg string `yaml:"event_subject_reg,omitempty" json:"event_subject_reg,omitempty"`
	// Receivers that will send to.
	Receivers []string `yaml:"receivers,omitempty" json:"receivers,omitempty"`
}

func (c *TektonNotification) IsMatched(typ, subject string) bool {
	if (c.EventType == "" || c.EventType == "*") || c.EventType == typ {
		if c.EventSubjectReg == "" {
			return true
		}

		ok, _ := regexp.MatchString(c.EventSubjectReg, subject)
		return ok
	}

	return false
}

type Tekton struct {
	DashboardBaseURL    string               `yaml:"dashboard_base_url,omitempty" json:"dashboard_base_url,omitempty"`
	Notifications       []TektonNotification `yaml:"notifications,omitempty" json:"notifications,omitempty"`
	FailedStepTailLines int                  `yaml:"failed_step_tail_lines,omitempty" json:"failed_step_tail_lines,omitempty"`
}

type Kafka struct {
	Brokers        []string             `yaml:"brokers,omitempty" json:"brokers,omitempty"`
	ClientID       string               `yaml:"client_id,omitempty" json:"client_id,omitempty"`
	Authentication kafka.Authentication `yaml:"authentication,omitempty" json:"authentication,omitempty"`
	Producer       kafka.Producer       `yaml:"producer,omitempty" json:"producer,omitempty"`
	Consumer       kafka.Consumer       `yaml:"consumer,omitempty" json:"consumer,omitempty"`
}

type TiBuild struct {
	ResultSinkURL  string `yaml:"result_sink_url,omitempty" json:"result_sink_url,omitempty"`
	TriggerSinkURL string `yaml:"trigger_sink_url,omitempty" json:"trigger_sink_url,omitempty"`
}

type Config struct {
	Store   Store      `yaml:"store,omitempty" json:"store,omitempty"`
	Lark    LarkBotApp `yaml:"lark,omitempty" json:"lark,omitempty"`
	Tekton  Tekton     `yaml:"tekton,omitempty" json:"tekton,omitempty"`
	TiBuild TiBuild    `yaml:"tibuild,omitempty" json:"tibuild,omitempty"`
	Kafka   Kafka      `yaml:"kafka,omitempty" json:"kafka,omitempty"`
}

func (c *Config) LoadFromFile(file string) error {
	content, err := os.ReadFile(file)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	ext := filepath.Ext(file)
	switch ext {
	case ".yaml", ".yml":
		return yaml.Unmarshal(content, c)
	case ".json":
		return json.Unmarshal(content, c)
	default:
		return fmt.Errorf("unsupported file format: %s", ext)
	}
}
