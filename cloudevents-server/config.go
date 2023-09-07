package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
)

type config struct {
	Store struct {
		Driver string `yaml:"driver,omitempty" json:"driver,omitempty"`
		DSN    string `yaml:"dsn,omitempty" json:"dsn,omitempty"`
	} `yaml:"store,omitempty" json:"store,omitempty"`
}

func (c *config) LoadFromFile(file string) error {
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

func newStoreClient(ctx context.Context, cfg config) (*ent.Client, error) {
	db, err := ent.Open(cfg.Store.Driver, cfg.Store.DSN)
	if err != nil {
		return nil, fmt.Errorf("failed opening connection to %s: %w", cfg.Store.Driver, err)
	}

	// Run the auto migration tool.
	if err := db.Schema.Create(ctx); err != nil {
		log.Fatalf("failed creating schema resources: %v", err)
	}

	return db, nil
}
