package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

type Store struct {
	Driver string `yaml:"driver,omitempty" json:"driver,omitempty"`
	DSN    string `yaml:"dsn,omitempty" json:"dsn,omitempty"`
}

type LarkBotApp struct {
	AppID     string `yaml:"app_id,omitempty" json:"app_id,omitempty"`
	AppSecret string `yaml:"app_secret,omitempty" json:"app_secret,omitempty"`
}

type Tekton struct {
	DashboardBaseURL string `yaml:"dashboard_base_url,omitempty" json:"dashboard_base_url,omitempty"`
	// Receivers receivers list of the event type, if you want it send all types, set the key "*".
	Receivers map[string][]string `yaml:"receivers,omitempty" json:"receiver,omitempty"`
}

type Config struct {
	Store   Store      `yaml:"store,omitempty" json:"store,omitempty"`
	Lark    LarkBotApp `yaml:"lark,omitempty" json:"lark,omitempty"`
	Tekton  Tekton     `yaml:"tekton,omitempty" json:"tekton,omitempty"`
	TiBuild struct {
		ResultSinkURL  string `yaml:"result_sink_url,omitempty" json:"result_sink_url,omitempty"`
		TriggerSinkURL string `yaml:"trigger_sink_url,omitempty" json:"trigger_sink_url,omitempty"`
	} `yaml:"tibuild,omitempty" json:"tibuild,omitempty"`
}

func (c *Config) LoadFromFile(file string) error {
	content, err := os.ReadFile(file)
	if err != nil {
		return fmt.Errorf("Failed to read file: %w", err)
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
