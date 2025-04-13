package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config represents the application configuration
type Config struct {
	// Bot configuration
	BotName string `yaml:"bot_name" json:"bot_name"`

	// Cherry pick configuration
	CherryPickInvite struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		GithubToken string `yaml:"github_token" json:"github_token"`
	} `yaml:"cherry_pick_invite" json:"cherry_pick_invite"`

	// Ask command configuration
	Ask struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		LLM struct {
			AzureConfig *struct {
				APIKey     string `yaml:"api_key" json:"api_key"`
				BaseURL    string `yaml:"base_url" json:"base_url"`
				APIVersion string `yaml:"api_version" json:"api_version"`
			} `yaml:"azure_config" json:"azure_config"`
			Model        string `yaml:"model" json:"model"`
			SystemPrompt string `yaml:"system_prompt" json:"system_prompt"`
			MCPServers   map[string]struct {
				BaseURL string `yaml:"base_url" json:"base_url"`
			} `yaml:"mcp_servers" json:"mcp_servers"`
		} `yaml:"llm" json:"llm"`
	} `yaml:"ask" json:"ask"`

	// DevBuild configuration
	DevBuild struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		ApiURL string `yaml:"api_url" json:"api_url"`
	} `yaml:"devbuild" json:"devbuild"`

	// Debug mode
	Debug bool `yaml:"debug" json:"debug"`
}

type BaseCmdConfig struct {
	AuditWebhook string `yaml:"audit_webhook" json:"audit_webhook"`
}

// LoadConfig loads the configuration from the specified YAML file
func LoadConfig(path string) (*Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("failed to open config file: %w", err)
	}
	defer f.Close()

	var cfg Config
	err = yaml.NewDecoder(f).Decode(&cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to decode config file: %w", err)
	}

	return &cfg, nil
}

// Validate validates the configuration
func (c *Config) Validate() error {
	// Add validation logic as needed
	return nil
}

// SetDefaults sets default values for the configuration
func (c *Config) SetDefaults() {
	// Set defaults for DevBuild
	if c.DevBuild.ApiURL == "" {
		c.DevBuild.ApiURL = "https://tibuild.pingcap.net/api/devbuilds"
	}
}
