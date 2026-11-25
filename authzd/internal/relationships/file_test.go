package relationships

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeSnapshot(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "grants.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write snapshot: %v", err)
	}
	return path
}

func TestFileSourceReadsReviewedSnapshot(t *testing.T) {
	path := writeSnapshot(t, `{
  "source": "reviewed-grants",
  "revision": "git:9dc1cce",
  "generated_at": "2026-11-25T09:00:00Z",
  "complete": true,
  "tuples": [
    {"user": "team:release#member", "relation": "caller", "object": "capability:tibuild.devbuild.rerun"}
  ]
}`)
	source, err := NewFileSource(path)
	if err != nil {
		t.Fatalf("NewFileSource() error = %v", err)
	}
	snapshot, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch() error = %v", err)
	}
	if snapshot.Source != "reviewed-grants" || snapshot.Revision != "git:9dc1cce" || !snapshot.Complete {
		t.Fatalf("Fetch() = %+v", snapshot)
	}
	if !snapshot.GeneratedAt.Equal(time.Date(2026, time.November, 25, 9, 0, 0, 0, time.UTC)) || len(snapshot.Tuples) != 1 {
		t.Fatalf("Fetch() = %+v", snapshot)
	}
}

func TestFileSourceTreatsOmittedCompleteAsIncomplete(t *testing.T) {
	// A hand-edited file that forgets "complete" must never prune tuples.
	path := writeSnapshot(t, `{"source":"reviewed-grants","revision":"r1","generated_at":"2026-11-25T09:00:00Z"}`)
	source, err := NewFileSource(path)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch() error = %v", err)
	}
	if snapshot.Complete {
		t.Fatal("a snapshot without an explicit complete flag was treated as complete")
	}
	reconciler, err := NewReconciler(source, newFakeStore(caller("team:r#member", "capability:c")), reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(context.Background()); err == nil {
		t.Fatal("Reconcile() accepted a snapshot without complete=true")
	}
}

func TestFileSourceRejectsMalformedSnapshots(t *testing.T) {
	cases := map[string]string{
		"unknown field":    `{"source":"s","revision":"r1","generated_at":"2026-11-25T09:00:00Z","complete":true,"fallback":"allow"}`,
		"trailing value":   `{"source":"s","revision":"r1","generated_at":"2026-11-25T09:00:00Z","complete":true} {}`,
		"not an object":    `[]`,
		"empty document":   ``,
		"bad timestamp":    `{"source":"s","revision":"r1","generated_at":"yesterday","complete":true}`,
		"unknown user key": `{"source":"s","revision":"r1","generated_at":"2026-11-25T09:00:00Z","complete":true,"tuples":[{"subject":"x","relation":"caller","object":"capability:c"}]}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			source, err := NewFileSource(writeSnapshot(t, content))
			if err != nil {
				t.Fatalf("NewFileSource() error = %v", err)
			}
			if _, err := source.Fetch(context.Background()); err == nil {
				t.Fatalf("Fetch() accepted %s", content)
			}
		})
	}
}

func TestFileSourceBoundsTheSnapshotSize(t *testing.T) {
	path := writeSnapshot(t, `{"source":"s","revision":"r1","generated_at":"2026-11-25T09:00:00Z","complete":true,"padding":"`+strings.Repeat("x", maxSnapshotBytes)+`"}`)
	source, err := NewFileSource(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := source.Fetch(context.Background()); err == nil {
		t.Fatal("Fetch() read a snapshot above the configured bound")
	}
}

func TestFileSourceRequiresAPathAndReportsReadFailures(t *testing.T) {
	if _, err := NewFileSource("  "); err == nil {
		t.Fatal("NewFileSource() accepted an empty path")
	}
	source, err := NewFileSource(filepath.Join(t.TempDir(), "missing.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := source.Fetch(context.Background()); err == nil {
		t.Fatal("Fetch() succeeded for a missing file")
	}
}
