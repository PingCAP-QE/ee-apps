package logserver

import (
	"errors"
	"os"
	"testing"
	"time"
)

func TestStoreAppendMetaFinalize(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	size, err := store.Append("ee-cd", "macbuild-abc", 0, []byte("hello"))
	if err != nil || size != 5 {
		t.Fatalf("append: size=%d err=%v", size, err)
	}
	if _, err := store.Append("ee-cd", "macbuild-abc", 0, []byte("hello")); err != nil {
		t.Fatalf("idempotent append: %v", err)
	}
	if _, err := store.Append("ee-cd", "macbuild-abc", 5, []byte(" world")); err != nil {
		t.Fatalf("second append: %v", err)
	}

	meta, err := store.Meta("ee-cd", "macbuild-abc")
	if err != nil {
		t.Fatalf("meta: %v", err)
	}
	if meta.Size != 11 || meta.Complete {
		t.Fatalf("unexpected meta before finalize: %#v", meta)
	}

	meta, err = store.Finalize("ee-cd", "macbuild-abc")
	if err != nil {
		t.Fatalf("finalize: %v", err)
	}
	if !meta.Complete || meta.CompletedAt == nil {
		t.Fatalf("unexpected finalized meta: %#v", meta)
	}
}

func TestStoreRejectsConflictingOffset(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	if _, err := store.Append("ee-cd", "macbuild-abc", 0, []byte("hello")); err != nil {
		t.Fatalf("append: %v", err)
	}
	if _, err := store.Append("ee-cd", "macbuild-abc", 1, []byte("x")); !errors.Is(err, ErrOffsetConflict) {
		t.Fatalf("expected offset conflict, got %v", err)
	}
}

func TestStoreReadTail(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	if _, err := store.Append("ee-cd", "macbuild-abc", 0, []byte("a\nb\nc")); err != nil {
		t.Fatalf("append: %v", err)
	}
	data, err := store.ReadTail("ee-cd", "macbuild-abc", 2)
	if err != nil {
		t.Fatalf("read tail: %v", err)
	}
	if string(data) != "b\nc" {
		t.Fatalf("unexpected tail: %q", data)
	}
}

func TestStoreRejectsInvalidNames(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	if _, err := store.Append("../escape", "macbuild-abc", 0, []byte("x")); err == nil {
		t.Fatal("expected an error for a path traversal namespace")
	}
	if _, err := store.Meta("ee-cd", "bad/name"); err == nil {
		t.Fatal("expected an error for an invalid build name")
	}
}

func TestStoreGC(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	for _, name := range []string{"old", "new", "running"} {
		if _, err := store.Append("ee-cd", name, 0, []byte("data")); err != nil {
			t.Fatalf("append %s: %v", name, err)
		}
	}
	if _, err := store.Finalize("ee-cd", "old"); err != nil {
		t.Fatalf("finalize old: %v", err)
	}
	if _, err := store.Finalize("ee-cd", "new"); err != nil {
		t.Fatalf("finalize new: %v", err)
	}

	oldPath, err := store.logPath("ee-cd", "old")
	if err != nil {
		t.Fatalf("log path: %v", err)
	}
	oldTime := now.Add(-48 * time.Hour)
	if err := os.Chtimes(oldPath, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes old: %v", err)
	}

	removed, err := store.GC(24*time.Hour, 0, now)
	if err != nil {
		t.Fatalf("gc: %v", err)
	}
	if removed != 1 {
		t.Fatalf("expected one removed log, got %d", removed)
	}
	if _, err := os.Stat(oldPath); !os.IsNotExist(err) {
		t.Fatalf("old log still exists: %v", err)
	}
	newPath, _ := store.logPath("ee-cd", "new")
	if _, err := os.Stat(newPath); err != nil {
		t.Fatalf("new log was removed: %v", err)
	}
	runningPath, _ := store.logPath("ee-cd", "running")
	if _, err := os.Stat(runningPath); err != nil {
		t.Fatalf("unfinished log was removed: %v", err)
	}
}

func TestStoreReset(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	if _, err := store.Append("ee-cd", "macbuild-abc", 0, []byte("hello")); err != nil {
		t.Fatalf("append: %v", err)
	}
	if err := store.Reset("ee-cd", "macbuild-abc"); err != nil {
		t.Fatalf("reset: %v", err)
	}
	if _, err := store.Meta("ee-cd", "macbuild-abc"); !os.IsNotExist(err) {
		t.Fatalf("expected the log to be removed, got %v", err)
	}
	if err := store.Reset("ee-cd", "macbuild-abc"); err != nil {
		t.Fatalf("reset missing log: %v", err)
	}
}

func TestStoreGCMaxBytes(t *testing.T) {
	t.Parallel()

	store := NewStore(t.TempDir())
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	for _, name := range []string{"oldest", "newest"} {
		if _, err := store.Append("ee-cd", name, 0, []byte("0123456789")); err != nil {
			t.Fatalf("append %s: %v", name, err)
		}
		if _, err := store.Finalize("ee-cd", name); err != nil {
			t.Fatalf("finalize %s: %v", name, err)
		}
	}
	oldest, err := store.logPath("ee-cd", "oldest")
	if err != nil {
		t.Fatalf("log path: %v", err)
	}
	oldTime := now.Add(-time.Hour)
	if err := os.Chtimes(oldest, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	removed, err := store.GC(0, 12, now)
	if err != nil {
		t.Fatalf("gc: %v", err)
	}
	if removed != 1 {
		t.Fatalf("expected one log removed by the size cap, got %d", removed)
	}
	if _, err := os.Stat(oldest); !os.IsNotExist(err) {
		t.Fatalf("oldest log still exists: %v", err)
	}
}
