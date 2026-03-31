package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Load and parse configuration
func Load[T any](configFile string) (*T, error) {
	var out T
	configData, err := os.ReadFile(configFile)
	if err != nil {
		return nil, fmt.Errorf("error reading config file: %v", err)
	}
	if err := yaml.Unmarshal(configData, &out); err != nil {
		return nil, fmt.Errorf("error parsing config file: %v", err)
	}
	return &out, nil
}
