package gitcred

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// fileConfig mirrors Config for on-disk configuration. A Tekton step writes it
// once and the credential helper reads it on every git invocation, so build
// steps need no environment variables.
type fileConfig struct {
	BrokerURL               string            `json:"broker_url"`
	TokenFile               string            `json:"token_file,omitempty"`
	CacheDir                string            `json:"cache_dir,omitempty"`
	Permissions             map[string]string `json:"permissions,omitempty"`
	Hosts                   []string          `json:"hosts,omitempty"`
	FallbackCredentialsFile string            `json:"fallback_credentials_file,omitempty"`
	StateFile               string            `json:"state_file,omitempty"`
	Timeout                 string            `json:"timeout,omitempty"`
}

// ConfigFromFile loads a JSON configuration file written by the Tekton
// setup-git-auth step.
func ConfigFromFile(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("gitcred: read config file: %w", err)
	}
	var file fileConfig
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&file); err != nil {
		return Config{}, fmt.Errorf("gitcred: decode config file: %w", err)
	}
	cfg := Config{
		BrokerURL:               strings.TrimSpace(file.BrokerURL),
		TokenFile:               strings.TrimSpace(file.TokenFile),
		CacheDir:                strings.TrimSpace(file.CacheDir),
		Permissions:             file.Permissions,
		Hosts:                   file.Hosts,
		FallbackCredentialsFile: strings.TrimSpace(file.FallbackCredentialsFile),
		StateFile:               strings.TrimSpace(file.StateFile),
	}
	if cfg.TokenFile == "" {
		cfg.TokenFile = defaultCallerTokenFile
	}
	if raw := strings.TrimSpace(file.Timeout); raw != "" {
		timeout, err := time.ParseDuration(raw)
		if err != nil {
			return Config{}, fmt.Errorf("gitcred: parse timeout: %w", err)
		}
		cfg.Timeout = timeout
	}
	return cfg, nil
}
