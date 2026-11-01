package state

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestStoreSaveLoadClear(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "nested", "state.json"))
	want := State{
		Namespace:      "ee-cd",
		Name:           "macbuild-abc",
		AssignmentID:   "agent-uid",
		Phase:          "Building",
		Workspace:      "/tmp/workspace",
		StartedAt:      time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC),
		LogFile:        "/tmp/build.log",
		UploadedOffset: 4096,
	}
	if err := store.Save(want); err != nil {
		t.Fatalf("save: %v", err)
	}
	if _, err := os.Stat(store.Path()); err != nil {
		t.Fatalf("state file missing: %v", err)
	}

	got, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got == nil || *got != want {
		t.Fatalf("unexpected state:\nwant %#v\n got %#v", want, got)
	}

	if err := store.Clear(); err != nil {
		t.Fatalf("clear: %v", err)
	}
	got, err = store.Load()
	if err != nil {
		t.Fatalf("load after clear: %v", err)
	}
	if got != nil {
		t.Fatalf("expected nil state after clear, got %#v", got)
	}
}

func TestStoreLoadMissing(t *testing.T) {
	t.Parallel()

	store := NewStore(filepath.Join(t.TempDir(), "missing.json"))
	got, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got != nil {
		t.Fatalf("expected nil state, got %#v", got)
	}
}

func TestStoreLoadCorrupt(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("{"), 0o600); err != nil {
		t.Fatalf("write corrupt state: %v", err)
	}
	if _, err := NewStore(path).Load(); err == nil {
		t.Fatal("expected a decode error")
	}
}

func TestStoreSaveLeavesNoTempFiles(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "state.json"))
	if err := store.Save(State{Namespace: "ee-cd", Name: "macbuild-abc", Phase: "Building"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read dir: %v", err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".state-") {
			t.Fatalf("temporary state file was left behind: %s", entry.Name())
		}
	}
}

func TestDefaultPath(t *testing.T) {
	t.Parallel()

	path, err := DefaultPath()
	if err != nil {
		t.Fatalf("default path: %v", err)
	}
	if !strings.HasSuffix(path, filepath.Join("macbuild-agent", "state.json")) {
		t.Fatalf("unexpected default path: %s", path)
	}
}

func TestStoreSaveEmptyPath(t *testing.T) {
	t.Parallel()

	if err := NewStore("").Save(State{Namespace: "ee-cd", Name: "macbuild-abc"}); err == nil {
		t.Fatal("expected an error for an empty state path")
	}
}

func TestStoreLoadMissingIdentity(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte(`{"phase":"Building"}`), 0o600); err != nil {
		t.Fatalf("write state: %v", err)
	}
	if _, err := NewStore(path).Load(); err == nil {
		t.Fatal("expected an error for state without a build identity")
	}
}

func TestStoreClearMissing(t *testing.T) {
	t.Parallel()

	store := NewStore(filepath.Join(t.TempDir(), "missing.json"))
	if err := store.Clear(); err != nil {
		t.Fatalf("clear missing state: %v", err)
	}
}

func TestStorePath(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "state.json")
	if got := NewStore(path).Path(); got != path {
		t.Fatalf("unexpected path: %q", got)
	}
}
