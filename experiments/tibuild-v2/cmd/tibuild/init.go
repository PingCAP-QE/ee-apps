package main

import (
	"context"
	"fmt"
	"os"

	"github.com/rs/zerolog/log"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// Load and parse configuration
func loadConfig(configFile string) (config.Service, error) {
	var config config.Service
	{
		configData, err := os.ReadFile(configFile)
		if err != nil {
			return config, fmt.Errorf("error reading config file: %v", err)
		}
		if err := yaml.Unmarshal(configData, &config); err != nil {
			return config, fmt.Errorf("error parsing config file: %v", err)
		}
	}
	return config, nil
}

func newStoreClient(cfg config.Service) (*ent.Client, error) {
	db, err := ent.Open(cfg.Driver, cfg.DSN)
	if err != nil {
		log.Err(err).Msgf("failed opening connection to %s", cfg.Driver)
		return nil, err
	}

	// Run the auto migration tool.
	if err := db.Schema.Create(context.Background()); err != nil {
		log.Err(err).Msg("failed creating schema resources")
		return nil, err
	}

	return db, nil
}
