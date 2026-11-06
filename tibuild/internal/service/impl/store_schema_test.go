package impl

import (
	"context"
	"errors"
	"testing"

	"github.com/go-sql-driver/mysql"
	"github.com/stretchr/testify/require"
)

func TestCreateSchemaRetriesWhenAnotherReplicaWonTheRace(t *testing.T) {
	var attempts int
	create := func(context.Context) error {
		attempts++
		if attempts < 2 {
			return &mysql.MySQLError{Number: 1050, Message: "Table 'tibuild.publish_step' already exists"}
		}
		return nil
	}

	require.NoError(t, createSchemaWith(context.Background(), create))
	require.Equal(t, 2, attempts, "the migration is retried after the duplicate error")
}

func TestCreateSchemaGivesUpAfterTheRetryBudget(t *testing.T) {
	var attempts int
	create := func(context.Context) error {
		attempts++
		return &mysql.MySQLError{Number: 1061, Message: "Duplicate key name 'idx_claim'"}
	}

	require.Error(t, createSchemaWith(context.Background(), create))
	require.Equal(t, schemaCreateAttempts, attempts)
}

func TestCreateSchemaDoesNotRetryRealErrors(t *testing.T) {
	var attempts int
	create := func(context.Context) error {
		attempts++
		return &mysql.MySQLError{Number: 1045, Message: "Access denied"}
	}

	err := createSchemaWith(context.Background(), create)
	require.Error(t, err)
	require.Equal(t, 1, attempts, "a permission error is not a race")
}

func TestIsDuplicateSchemaError(t *testing.T) {
	tests := map[string]struct {
		err  error
		want bool
	}{
		"nil":              {nil, false},
		"table exists":     {&mysql.MySQLError{Number: 1050}, true},
		"duplicate index":  {&mysql.MySQLError{Number: 1061}, true},
		"duplicate fk":     {&mysql.MySQLError{Number: 1826}, true},
		"access denied":    {&mysql.MySQLError{Number: 1045}, false},
		"sqlite duplicate": {errors.New(`sql/schema: create table "publish_step": table publish_step already exists`), true},
		"other":            {errors.New("dial tcp: connection refused"), false},
	}
	for name, tt := range tests {
		require.Equal(t, tt.want, isDuplicateSchemaError(tt.err), name)
	}
}
