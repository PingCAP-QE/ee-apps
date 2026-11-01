package logging

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"k8s.io/client-go/rest"
)

const (
	defaultChunkSize = 64 * 1024
	defaultInterval  = 2 * time.Second
)

// RemoteMeta mirrors the log server's metadata response.
type RemoteMeta struct {
	Size        int64      `json:"size"`
	Complete    bool       `json:"complete"`
	UpdatedAt   time.Time  `json:"updatedAt"`
	CompletedAt *time.Time `json:"completedAt,omitempty"`
}

// Uploader sends a local build log to the operator log service in chunks.
type Uploader struct {
	Log       *Log
	Client    *http.Client
	URL       string
	ChunkSize int
	Interval  time.Duration
	// MaxBackoff caps the exponential retry delay (default 30s).
	MaxBackoff time.Duration
}

// NewUploader builds an uploader that reaches the log service through the API
// server service proxy.
func NewUploader(
	config *rest.Config,
	serviceNamespace, serviceName string,
	servicePort int,
	buildNamespace, buildName string,
	log *Log,
) (*Uploader, error) {
	if config == nil {
		return nil, fmt.Errorf("rest config is nil")
	}
	if servicePort <= 0 {
		servicePort = 8080
	}
	transport, err := rest.TransportFor(config)
	if err != nil {
		return nil, fmt.Errorf("build API transport: %w", err)
	}
	base := strings.TrimRight(config.Host, "/")
	url := fmt.Sprintf(
		"%s/api/v1/namespaces/%s/services/http:%s:%d/proxy/v1/logs/%s/%s",
		base, serviceNamespace, serviceName, servicePort, buildNamespace, buildName,
	)
	return &Uploader{
		Log:       log,
		Client:    &http.Client{Transport: transport, Timeout: 30 * time.Second},
		URL:       url,
		ChunkSize: defaultChunkSize,
		Interval:  defaultInterval,
	}, nil
}

// Run uploads new bytes until the context is cancelled.
func (u *Uploader) Run(ctx context.Context) error {
	if u.Log == nil {
		return fmt.Errorf("log is nil")
	}
	_ = u.Reset(ctx)
	offset := int64(0)
	delay := u.interval()
	for {
		advanced, _ := u.uploadOnce(ctx, &offset)
		if advanced {
			delay = u.interval()
			continue
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
		delay *= 2
		if max := u.maxBackoff(); delay > max {
			delay = max
		}
	}
}

// Reset removes a previous attempt's log for this build.
func (u *Uploader) Reset(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, u.URL, nil)
	if err != nil {
		return err
	}
	resp, err := u.client().Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	switch resp.StatusCode {
	case http.StatusOK, http.StatusNoContent, http.StatusNotFound:
		return nil
	default:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4*1024))
		return fmt.Errorf("reset log: status %d: %s", resp.StatusCode, body)
	}
}

// Finalize uploads the remaining bytes and marks the remote log complete.
func (u *Uploader) Finalize(ctx context.Context) (RemoteMeta, error) {
	offset := int64(0)
	for {
		advanced, err := u.uploadOnce(ctx, &offset)
		if err != nil {
			return RemoteMeta{}, err
		}
		if !advanced {
			break
		}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.URL+"/finalize", nil)
	if err != nil {
		return RemoteMeta{}, err
	}
	resp, err := u.client().Do(req)
	if err != nil {
		return RemoteMeta{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4*1024))
		return RemoteMeta{}, fmt.Errorf("finalize log: status %d: %s", resp.StatusCode, body)
	}
	var meta RemoteMeta
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		return RemoteMeta{}, err
	}
	return meta, nil
}

func (u *Uploader) uploadOnce(ctx context.Context, offset *int64) (bool, error) {
	file, err := os.Open(u.Log.Path())
	if err != nil {
		return false, err
	}
	defer func() { _ = file.Close() }()

	info, err := file.Stat()
	if err != nil {
		return false, err
	}
	if info.Size() <= *offset {
		return false, nil
	}

	size := u.chunkSize()
	remaining := info.Size() - *offset
	if remaining < int64(size) {
		size = int(remaining)
	}
	data := make([]byte, size)
	n, err := file.ReadAt(data, *offset)
	if err != nil && err != io.EOF {
		return false, err
	}
	if n == 0 {
		return false, nil
	}
	data = data[:n]

	sum := sha256.Sum256(data)
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPut,
		fmt.Sprintf("%s?offset=%d", u.URL, *offset),
		bytes.NewReader(data),
	)
	if err != nil {
		return false, err
	}
	req.Header.Set("X-Chunk-SHA256", hex.EncodeToString(sum[:]))
	resp, err := u.client().Do(req)
	if err != nil {
		return false, err
	}
	defer func() { _ = resp.Body.Close() }()

	switch resp.StatusCode {
	case http.StatusOK:
		var response struct {
			Size int64 `json:"size"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&response); err != nil {
			return false, err
		}
		*offset = response.Size
		return true, nil
	case http.StatusConflict:
		var response struct {
			Size int64 `json:"size"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&response); err != nil {
			return false, err
		}
		*offset = response.Size
		return true, nil
	default:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4*1024))
		return false, fmt.Errorf("upload log chunk: status %d: %s", resp.StatusCode, body)
	}
}

func (u *Uploader) client() *http.Client {
	if u.Client != nil {
		return u.Client
	}
	return http.DefaultClient
}

func (u *Uploader) chunkSize() int {
	if u.ChunkSize > 0 {
		return u.ChunkSize
	}
	return defaultChunkSize
}

func (u *Uploader) interval() time.Duration {
	if u.Interval > 0 {
		return u.Interval
	}
	return defaultInterval
}

func (u *Uploader) maxBackoff() time.Duration {
	if u.MaxBackoff > 0 {
		return u.MaxBackoff
	}
	return 30 * time.Second
}
