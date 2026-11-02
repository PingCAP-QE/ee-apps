package logserver

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"k8s.io/apimachinery/pkg/util/validation"
)

// ErrOffsetConflict means the chunk does not line up with the stored log.
var ErrOffsetConflict = errors.New("log offset conflict")

// Meta describes a stored build log.
type Meta struct {
	Size        int64      `json:"size"`
	Complete    bool       `json:"complete"`
	UpdatedAt   time.Time  `json:"updatedAt"`
	CompletedAt *time.Time `json:"completedAt,omitempty"`
	Truncated   bool       `json:"truncated,omitempty"`
}

type completion struct {
	Size        int64     `json:"size"`
	CompletedAt time.Time `json:"completedAt"`
}

// Store persists one append-only log file per build.
type Store struct {
	dir string
	mu  sync.Mutex
}

// NewStore returns a store rooted at dir.
func NewStore(dir string) *Store {
	return &Store{dir: dir}
}

// Dir returns the storage root.
func (s *Store) Dir() string {
	return s.dir
}

func (s *Store) logPath(namespace, name string) (string, error) {
	if err := validatePathPart(namespace); err != nil {
		return "", fmt.Errorf("namespace: %w", err)
	}
	if err := validatePathPart(name); err != nil {
		return "", fmt.Errorf("name: %w", err)
	}
	// Hash the user-provided names so the on-disk path never contains them.
	sum := sha256.Sum256([]byte(namespace + "/" + name))
	return filepath.Join(s.dir, hex.EncodeToString(sum[:])+".log"), nil
}

func (s *Store) donePath(namespace, name string) (string, error) {
	path, err := s.logPath(namespace, name)
	if err != nil {
		return "", err
	}
	return path + ".done", nil
}

func validatePathPart(value string) error {
	if value == "" || value == "." || value == ".." || filepath.Base(value) != value {
		return fmt.Errorf("invalid path segment %q", value)
	}
	if errs := validation.IsDNS1123Subdomain(value); len(errs) > 0 {
		return fmt.Errorf("invalid DNS name %q: %v", value, errs)
	}
	return nil
}

// Append writes data at offset. Re-sending an identical chunk is idempotent;
// any other overlap is reported as ErrOffsetConflict.
func (s *Store) Append(namespace, name string, offset int64, data []byte) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	path, err := s.logPath(namespace, name)
	if err != nil {
		return 0, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return 0, fmt.Errorf("create log dir: %w", err)
	}

	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return 0, fmt.Errorf("open log: %w", err)
	}
	defer func() { _ = file.Close() }()

	info, err := file.Stat()
	if err != nil {
		return 0, fmt.Errorf("stat log: %w", err)
	}
	size := info.Size()
	if offset < 0 || offset > size {
		return size, ErrOffsetConflict
	}
	if offset < size {
		overlap := size - offset
		if overlap > int64(len(data)) {
			overlap = int64(len(data))
		}
		existing := make([]byte, overlap)
		if _, err := file.ReadAt(existing, offset); err != nil && !errors.Is(err, io.EOF) {
			return size, fmt.Errorf("read existing chunk: %w", err)
		}
		if !bytes.Equal(existing, data[:overlap]) {
			return size, ErrOffsetConflict
		}
		// A retry may resend a chunk that grew since the previous upload.
		// Keep the matching prefix and append only the new suffix.
		if int64(len(data)) <= size-offset {
			return size, nil
		}
		data = data[overlap:]
		offset = size
	}

	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return size, fmt.Errorf("seek log: %w", err)
	}
	if _, err := file.Write(data); err != nil {
		return size, fmt.Errorf("append log: %w", err)
	}
	if err := file.Sync(); err != nil {
		return size, fmt.Errorf("sync log: %w", err)
	}
	return offset + int64(len(data)), nil
}

// Reset removes any previous attempt's log for this build.
func (s *Store) Reset(namespace, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	path, err := s.logPath(namespace, name)
	if err != nil {
		return err
	}
	for _, file := range []string{path, path + ".done"} {
		if err := os.Remove(file); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

// Meta returns the log metadata, or os.ErrNotExist when there is no log.
func (s *Store) Meta(namespace, name string) (Meta, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	path, err := s.logPath(namespace, name)
	if err != nil {
		return Meta{}, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return Meta{}, err
	}
	meta := Meta{Size: info.Size(), UpdatedAt: info.ModTime()}

	donePath, err := s.donePath(namespace, name)
	if err != nil {
		return Meta{}, err
	}
	data, err := os.ReadFile(donePath)
	if err == nil {
		var done completion
		if json.Unmarshal(data, &done) == nil {
			meta.Complete = done.Size == info.Size()
			meta.CompletedAt = &done.CompletedAt
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return Meta{}, fmt.Errorf("read completion: %w", err)
	}
	return meta, nil
}

// Finalize marks a log complete at the current size.
func (s *Store) Finalize(namespace, name string) (Meta, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	path, err := s.logPath(namespace, name)
	if err != nil {
		return Meta{}, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return Meta{}, err
	}
	now := time.Now().UTC()
	done := completion{Size: info.Size(), CompletedAt: now}
	data, err := json.Marshal(done)
	if err != nil {
		return Meta{}, err
	}
	donePath, err := s.donePath(namespace, name)
	if err != nil {
		return Meta{}, err
	}
	tmp, err := os.CreateTemp(filepath.Dir(donePath), ".done-*.tmp")
	if err != nil {
		return Meta{}, err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return Meta{}, err
	}
	if err := tmp.Close(); err != nil {
		return Meta{}, err
	}
	if err := os.Rename(tmpName, donePath); err != nil {
		return Meta{}, err
	}

	return Meta{Size: info.Size(), Complete: true, UpdatedAt: info.ModTime(), CompletedAt: &now}, nil
}

// Open returns the log file for streaming. The caller closes it.
func (s *Store) Open(namespace, name string) (*os.File, error) {
	path, err := s.logPath(namespace, name)
	if err != nil {
		return nil, err
	}
	return os.Open(path)
}

// ReadTail returns the last tail lines (or the whole log when tail <= 0).
func (s *Store) ReadTail(namespace, name string, tail int) ([]byte, error) {
	path, err := s.logPath(namespace, name)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if tail <= 0 {
		return data, nil
	}
	lines := bytes.Split(data, []byte("\n"))
	if len(lines) <= tail {
		return data, nil
	}
	return bytes.Join(lines[len(lines)-tail:], []byte("\n")), nil
}

// GC removes completed logs older than retention and trims the oldest completed
// logs until the store fits maxBytes.
func (s *Store) GC(retention time.Duration, maxBytes int64, now time.Time) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return 0, fmt.Errorf("create log store: %w", err)
	}
	type candidate struct {
		path    string
		modTime time.Time
		size    int64
	}
	var candidates []candidate
	var total int64

	err := filepath.WalkDir(s.dir, func(path string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return nil
		}
		if filepath.Ext(path) != ".log" {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return nil
		}
		if _, err := os.Stat(path + ".done"); err != nil {
			return nil // do not remove a log that may still be running
		}
		candidates = append(candidates, candidate{path: path, modTime: info.ModTime(), size: info.Size()})
		total += info.Size()
		return nil
	})
	if err != nil {
		return 0, err
	}

	removed := 0
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].modTime.Before(candidates[j].modTime) })
	for _, candidate := range candidates {
		expired := retention > 0 && now.Sub(candidate.modTime) > retention
		overLimit := maxBytes > 0 && total > maxBytes
		if !expired && !overLimit {
			continue
		}
		if err := os.Remove(candidate.path); err != nil {
			continue
		}
		_ = os.Remove(candidate.path + ".done")
		total -= candidate.size
		removed++
	}
	return removed, nil
}
