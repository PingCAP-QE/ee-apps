package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"

	_ "github.com/go-sql-driver/mysql"
)

// Load and parse configuration
func loadConfig(configFile string) (*config.Service, error) {
	var config config.Service
	{
		configData, err := os.ReadFile(configFile)
		if err != nil {
			return nil, fmt.Errorf("error reading config file: %v", err)
		}
		if err := yaml.Unmarshal(configData, &config); err != nil {
			return nil, fmt.Errorf("error parsing config file: %v", err)
		}
	}
	return &config, nil
}
