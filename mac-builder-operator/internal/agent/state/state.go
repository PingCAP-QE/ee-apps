package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// State is the agent's persisted view of an in-flight build. It lets a restarted
// agent decide whether to rerun the build, fail it, or discard it.
type State struct {
	Namespace      string    `json:"namespace"`
	Name           string    `json:"name"`
	AssignmentID   string    `json:"assignmentID,omitempty"`
	Phase          string    `json:"phase"`
	Workspace      string    `json:"workspace,omitempty"`
	StartedAt      time.Time `json:"startedAt"`
	LogFile        string    `json:"logFile,omitempty"`
	UploadedOffset int64     `json:"uploadedOffset,omitempty"`
}

// Store persists a single build State with atomic writes.
type Store struct {
	path string
	mu   sync.Mutex
}

// NewStore returns a Store writing to path.
func NewStore(path string) *Store {
	return &Store{path: path}
}

// DefaultPath returns the per-user state path on macOS.
func DefaultPath() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("resolve user config dir: %w", err)
	}
	return filepath.Join(dir, "macbuild-agent", "state.json"), nil
}

// Path returns the configured state file path.
func (s *Store) Path() string {
	return s.path
}

// Load returns the persisted state, or nil when no state file exists.
func (s *Store) Load() (*State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read state: %w", err)
	}

	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("decode state: %w", err)
	}
	if state.Namespace == "" || state.Name == "" {
		return nil, fmt.Errorf("state is missing the build identity")
	}
	return &state, nil
}

// Save atomically writes the state file with mode 0600.
func (s *Store) Save(state State) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.path == "" {
		return fmt.Errorf("state path is empty")
	}
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}

	data, err := json.Marshal(state)
	if err != nil {
		return fmt.Errorf("encode state: %w", err)
	}

	tmp, err := os.CreateTemp(dir, ".state-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp state: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("chmod temp state: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp state: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp state: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp state: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("replace state: %w", err)
	}
	return nil
}

// Clear removes the state file. A missing file is not an error.
func (s *Store) Clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove state: %w", err)
	}
	return nil
}
