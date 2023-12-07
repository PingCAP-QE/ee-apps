package testcaserun

import (
	"context"
	"fmt"
	"log"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

func newStoreClient(cfg config.Store) (*ent.Client, error) {
	db, err := ent.Open(cfg.Driver, cfg.DSN)
	if err != nil {
		return nil, fmt.Errorf("failed opening connection to %s: %w", cfg.Driver, err)
	}

	// Run the auto migration tool.
	if err := db.Schema.Create(context.Background()); err != nil {
		log.Fatalf("failed creating schema resources: %v", err)
	}

	return db, nil
}
