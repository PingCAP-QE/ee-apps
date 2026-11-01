package workspace

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/go-logr/logr"
)

func TestCleanupRemovesOldWorkspacesOnly(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)

	oldDir := filepath.Join(root, "macbuild-old")
	newDir := filepath.Join(root, "macbuild-new")
	otherDir := filepath.Join(root, "keep-me")
	for _, dir := range []string{oldDir, newDir, otherDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", dir, err)
		}
	}
	oldTime := now.Add(-8 * 24 * time.Hour)
	if err := os.Chtimes(oldDir, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes old: %v", err)
	}
	if err := os.Chtimes(otherDir, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes other: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "macbuild-file"), []byte("x"), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}

	removed, err := Cleanup(root, "macbuild-", 7*24*time.Hour, now)
	if err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	if removed != 1 {
		t.Fatalf("expected one removed workspace, got %d", removed)
	}
	if _, err := os.Stat(oldDir); !os.IsNotExist(err) {
		t.Fatalf("old workspace still exists: %v", err)
	}
	for _, dir := range []string{newDir, otherDir} {
		if _, err := os.Stat(dir); err != nil {
			t.Fatalf("expected %s to remain: %v", dir, err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "macbuild-file")); err != nil {
		t.Fatalf("file with the workspace prefix must not be removed: %v", err)
	}
}

func TestCleanupRejectsEmptyPrefix(t *testing.T) {
	t.Parallel()

	if _, err := Cleanup(t.TempDir(), "", time.Hour, time.Now()); err == nil {
		t.Fatal("expected an error for an empty prefix")
	}
}

func TestCleanerRunRemovesStaleWorkspace(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	oldDir := filepath.Join(root, "macbuild-old")
	if err := os.MkdirAll(oldDir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	oldTime := now.Add(-8 * 24 * time.Hour)
	if err := os.Chtimes(oldDir, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	cleaner := &Cleaner{
		Root:   root,
		Prefix: "macbuild-",
		MaxAge: 7 * 24 * time.Hour,
		Now:    func() time.Time { return now },
	}
	if cleaner.NeedLeaderElection() {
		t.Fatal("workspace cleaner must run on every worker")
	}
	cleaner.run(logr.Discard())

	if _, err := os.Stat(oldDir); !os.IsNotExist(err) {
		t.Fatalf("stale workspace was not removed: %v", err)
	}
}

func TestCleanerStartStopsOnContextCancel(t *testing.T) {
	t.Parallel()

	cleaner := &Cleaner{
		Root:     t.TempDir(),
		Prefix:   "macbuild-",
		MaxAge:   time.Hour,
		Interval: time.Hour,
		Now:      func() time.Time { return time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC) },
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- cleaner.Start(ctx)
	}()
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("cleaner start: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("cleaner did not stop after context cancellation")
	}
}

func TestCleanupInvalidRoot(t *testing.T) {
	t.Parallel()

	if _, err := Cleanup(filepath.Join(t.TempDir(), "missing"), "macbuild-", time.Hour, time.Now()); err == nil {
		t.Fatal("expected an error for a missing workspace root")
	}
}
