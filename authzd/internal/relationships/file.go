package relationships

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// maxSnapshotBytes bounds the reviewed snapshot. Reviewed relationship data is
// small; a larger file is a mistake or an attack on the job's memory.
const maxSnapshotBytes = 1 << 20

// FileSource reads a reviewed snapshot from a local file. It is the bootstrap
// and reviewed-change path: the file is produced by a source with a known
// revision and committed or mounted by an operator.
type FileSource struct {
	path string
}

// NewFileSource validates the path. Reading happens per Fetch so a mounted
// ConfigMap or freshly written file is picked up without a restart.
func NewFileSource(path string) (*FileSource, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("snapshot path is required")
	}
	return &FileSource{path: strings.TrimSpace(path)}, nil
}

// Fetch parses exactly one snapshot document. Unknown fields, trailing values,
// and oversized files are rejected: a partially understood snapshot must not
// become an authoritative membership list.
func (s *FileSource) Fetch(ctx context.Context) (Snapshot, error) {
	if s == nil {
		return Snapshot{}, errors.New("snapshot source is not configured")
	}
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	file, err := os.Open(s.path)
	if err != nil {
		return Snapshot{}, fmt.Errorf("open snapshot: %w", err)
	}
	defer func() { _ = file.Close() }()
	data, err := io.ReadAll(io.LimitReader(file, maxSnapshotBytes+1))
	if err != nil {
		return Snapshot{}, fmt.Errorf("read snapshot: %w", err)
	}
	if len(data) > maxSnapshotBytes {
		return Snapshot{}, fmt.Errorf("snapshot exceeds %d bytes", maxSnapshotBytes)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var snapshot Snapshot
	if err := decoder.Decode(&snapshot); err != nil {
		return Snapshot{}, fmt.Errorf("decode snapshot: %w", err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return Snapshot{}, errors.New("snapshot must contain exactly one JSON document")
	}
	return snapshot, nil
}
