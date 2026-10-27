package config

import (
	"errors"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config represents the overall application configuration, including all subcommands.
type Config struct {
	// Bot configuration
	AppID             string `yaml:"app_id" json:"app_id"`
	AppSecret         string `yaml:"app_secret" json:"app_secret"`
	UserCustomAttrIDs *struct {
		GitHubID string `yaml:"github_id" json:"github_id"`
	} `yaml:"user_custom_attr_ids,omitempty" json:"user_custom_attr_ids,omitempty"`

	// Cherry pick configuration
	CherryPickInvite *struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		// GithubToken is the static token. Inviting collaborators needs
		// administration: write, which is still pending a governance review, so
		// this command keeps using a PAT.
		GithubToken string `yaml:"github_token" json:"github_token"`
	} `yaml:"cherry_pick_invite" json:"cherry_pick_invite"`

	// Ask command configuration
	Ask *struct {
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
	DevBuild *struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		ApiURL string `yaml:"api_url" json:"api_url"`
	} `yaml:"devbuild" json:"devbuild"`

	RegistryImage *RegistryImageConfig `yaml:"registry_image" json:"registry_image"`

	Hotfix *struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		ApiURL string `yaml:"api_url" json:"api_url"`
	} `yaml:"hotfix" json:"hotfix"`

	// RepoAdmin command configuration
	RepoAdmin *struct {
		BaseCmdConfig `yaml:",inline" json:",inline"`

		// GithubToken is the legacy static token used as the broker fallback.
		GithubToken string `yaml:"github_token" json:"github_token"`
	} `yaml:"repo_admins" json:"repo_admins"`

	// GitHub configures the shared GitHub App token broker client used by all
	// commands that call the GitHub API. When the block is absent the broker is
	// disabled and commands only work with a static token.
	GitHub *GitHubConfig `yaml:"github" json:"github"`

	// Debug mode
	Debug bool `yaml:"debug" json:"debug"`
}

type BaseCmdConfig struct {
	Audit *AuditConfig `yaml:"audit,omitempty" json:"audit,omitempty"`
}

type RegistryImageConfig struct {
	BaseCmdConfig `yaml:",inline" json:",inline"`

	Owner       string `yaml:"owner" json:"owner"`
	Repo        string `yaml:"repo" json:"repo"`
	Workflow    string `yaml:"workflow" json:"workflow"`
	Ref         string `yaml:"ref" json:"ref"`
	GitHubToken string `yaml:"github_token" json:"github_token"`
}

// GitHubConfig configures the in-cluster GitHub App token broker client.
type GitHubConfig struct {
	// BrokerURL is the broker base URL. When the github block is present and
	// this is empty, the in-cluster service DNS name is used.
	BrokerURL string `yaml:"broker_url" json:"broker_url"`
	// BrokerTokenFile is the projected ServiceAccount token used to
	// authenticate to the broker.
	BrokerTokenFile string `yaml:"broker_token_file" json:"broker_token_file"`
	// Token is an optional static token used only when the broker is disabled,
	// the request is not repository-scoped, or the broker denies the request.
	Token string `yaml:"token" json:"token"`
}

type AuditConfig struct {
	Webhook string `yaml:"webhook" json:"webhook"` // if empty, disable audit.
	Title   string `yaml:"title" json:"title"`     // if empty, use default title
	Result  bool   `yaml:"result" json:"result"`   // if true, include command result in audit
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
	if c.AppID == "" {
		return errors.New("app_id is required")
	}
	if c.AppSecret == "" {
		return errors.New("app_secret is required")
	}
	return nil
}
