package main

import (
	"context"
	"log"
	"os"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

func main() {
	logger := log.New(os.Stderr, "[delivery-approval-migrate] ", log.Ltime)
	dsn := os.Getenv("DL_APPROVAL_DATABASE_DSN")
	if dsn == "" {
		logger.Fatal("DL_APPROVAL_DATABASE_DSN is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	st, err := store.Open(ctx, dsn)
	if err != nil {
		logger.Fatal(err)
	}
	defer st.Close()
	if err := st.Migrate(ctx); err != nil {
		logger.Fatal("approval schema migration failed")
	}
	logger.Print("approval schema ready")
}
