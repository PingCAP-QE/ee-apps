package relationships

import (
	"context"
	"path/filepath"
	"testing"
)

// TestShippedExampleSnapshotIsAccepted keeps the documented example honest: it
// must parse, be complete, and contain only tuples inside the published scope.
func TestShippedExampleSnapshotIsAccepted(t *testing.T) {
	path := filepath.Join("..", "..", "policies", "capability-grants.example.json")
	source, err := NewFileSource(path)
	if err != nil {
		t.Fatalf("NewFileSource() error = %v", err)
	}
	snapshot, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch() error = %v", err)
	}
	if !snapshot.Complete || len(snapshot.Tuples) == 0 {
		t.Fatalf("example snapshot = %+v, want a complete snapshot with at least one grant", snapshot)
	}
	reconciler, err := NewReconciler(source, &recordingStore{}, Scope{
		ObjectTypes: []string{"capability"}, Relations: []string{"caller"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.WithDryRun(true).Reconcile(context.Background()); err != nil {
		t.Fatalf("the shipped example snapshot was rejected: %v", err)
	}
}

type recordingStore struct{}

func (recordingStore) Read(context.Context, string) ([]Tuple, error) { return nil, nil }
func (recordingStore) Apply(context.Context, ChangeSet) error        { return nil }
