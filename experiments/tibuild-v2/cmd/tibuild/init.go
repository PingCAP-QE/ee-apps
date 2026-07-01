package main

import (
	_ "github.com/go-sql-driver/mysql"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// loadConfig reads and parses the YAML config file, returning a thread-safe
// Reloadable wrapper that supports hot-reload via file polling or SIGHUP.
func loadConfig(configFile string) (*config.Reloadable, error) {
	return config.Load(configFile)
}
