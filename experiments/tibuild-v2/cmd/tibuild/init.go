package main

import (
	"os"

	_ "github.com/go-sql-driver/mysql"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// loadConfig reads, validates and watches the YAML config file. The returned
// watcher keeps the previous revision when a new document is rejected.
func loadConfig(configFile string) (*config.Watcher, error) {
	logger := zerolog.New(os.Stderr).With().Timestamp().Str("component", "config").Logger()
	return config.NewWatcher(configFile, config.WatcherOptions{
		Logger: &logger,
		// The HTTP middleware and the reconciler schedule are built once at
		// startup, so a change to them needs a rollout.
		ExtraRestartOnlyFields: []string{
			"identity_headers",
			"tekton.reconciler_interval",
		},
	})
}
