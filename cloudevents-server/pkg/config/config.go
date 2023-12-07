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

type Lark struct {
	AppID     string `yaml:"app_id,omitempty" json:"app_id,omitempty"`
	AppSecret string `yaml:"app_secret,omitempty" json:"app_secret,omitempty"`
	// TODO: how to get the receiver?
	Receiver string `yaml:"receiver,omitempty" json:"receiver,omitempty"`
}

type Config struct {
	Store Store `yaml:"store,omitempty" json:"store,omitempty"`
	Lark  Lark  `yaml:"lark,omitempty" json:"lark,omitempty"`
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
