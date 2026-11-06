package impl

import (
	"context"
	"errors"
	"strings"

	"github.com/go-sql-driver/mysql"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// schemaCreateAttempts bounds how often the auto migration is retried when it
// loses a race against another replica.
const schemaCreateAttempts = 3

// createSchema runs Ent's auto migration for the given client.
func createSchema(ctx context.Context, db *ent.Client) error {
	return createSchemaWith(ctx, func(ctx context.Context) error {
		return db.Schema.Create(ctx)
	})
}

// createSchemaWith retries the migration while it fails because another process
// created the object first.
//
// Ent's auto migration is not atomic across processes. The API and the worker
// restart together on a release, both introspect the database, both decide a
// table is missing, and the loser gets "Error 1050: Table ... already exists"
// (or 1061 for an index). The object then exists exactly as the winner created
// it, so retrying is the correct reaction — failing the process turns a benign
// race into a CrashLoop, which is what happened when `publish_step` was first
// created in the TKE cluster.
func createSchemaWith(ctx context.Context, create func(context.Context) error) error {
	var err error
	for attempt := 0; attempt < schemaCreateAttempts; attempt++ {
		err = create(ctx)
		if err == nil || !isDuplicateSchemaError(err) {
			return err
		}
	}
	return err
}

// isDuplicateSchemaError reports whether err comes from losing the schema race.
func isDuplicateSchemaError(err error) bool {
	if err == nil {
		return false
	}
	var mysqlErr *mysql.MySQLError
	if errors.As(err, &mysqlErr) {
		switch mysqlErr.Number {
		case 1050, // table already exists
			1061, // duplicate key name (index)
			1826: // duplicate foreign key constraint name
			return true
		}
		return false
	}
	// sqlite (unit tests) reports the same situation as a plain message.
	return strings.Contains(err.Error(), "already exists")
}
