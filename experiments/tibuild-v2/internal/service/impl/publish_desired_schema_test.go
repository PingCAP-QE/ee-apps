package impl

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// TestPublishDesiredUniqueChannel proves the logical primary key of the
// desired-state table: one row per (target, pkg, os, arch). The upsert in
// Phase 2 and the conditional claim in Phase 3 rely on a duplicate insert
// being rejected by the database, so this is the schema's acceptance test.
func TestPublishDesiredUniqueChannel(t *testing.T) {
	db := serverlessTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 30, 2, 0, 0, 0, time.UTC)

	create := func(arch, version string) error {
		_, err := db.PublishDesired.Create().
			SetTarget("tiup-prod").
			SetPkg("tidb").
			SetOs("linux").
			SetArch(arch).
			SetDesiredVersion(version).
			SetDesiredPayload(map[string]any{"version": version}).
			SetDesiredAt(now).
			SetNextRunAt(now).
			Save(ctx)
		return err
	}

	require.NoError(t, create("amd64", "v9.0.0-nightly"))

	err := create("amd64", "v9.0.1-nightly")
	require.Error(t, err, "a second row for the same channel must be rejected")
	require.True(t, ent.IsConstraintError(err), "expected a duplicate-key error, got %v", err)

	// A different architecture is a different channel and must be accepted.
	require.NoError(t, create("arm64", "v9.0.0-nightly"))

	count, err := db.PublishDesired.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 2, count)
}
