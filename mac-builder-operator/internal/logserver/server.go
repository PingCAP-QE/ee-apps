package logserver

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

const (
	defaultMaxBuildBytes int64 = 100 * 1024 * 1024
	defaultMaxStoreBytes int64 = 10 * 1024 * 1024 * 1024
	defaultRetention           = 7 * 24 * time.Hour
	maxChunkBytes        int64 = 1024 * 1024
)

// Config configures the log server.
type Config struct {
	Addr          string
	Store         *Store
	MaxBuildBytes int64
	MaxStoreBytes int64
	Retention     time.Duration
	Now           func() time.Time
}

// Server serves the build log HTTP API from inside the operator process.
type Server struct {
	config     Config
	httpServer *http.Server
}

// New returns a log server.
func New(config Config) *Server {
	if config.Store == nil {
		config.Store = NewStore("/var/lib/macbuild-logs")
	}
	if config.Addr == "" {
		config.Addr = ":8080"
	}
	if config.MaxBuildBytes <= 0 {
		config.MaxBuildBytes = defaultMaxBuildBytes
	}
	if config.MaxStoreBytes <= 0 {
		config.MaxStoreBytes = defaultMaxStoreBytes
	}
	if config.Retention <= 0 {
		config.Retention = defaultRetention
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Server{config: config}
}

// Start listens until the manager context is cancelled.
func (s *Server) Start(ctx context.Context) error {
	logger := logf.FromContext(ctx).WithName("log-server")

	listener, err := net.Listen("tcp", s.config.Addr)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", s.config.Addr, err)
	}
	s.httpServer = &http.Server{
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		if err := s.httpServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error(err, "log server stopped unexpectedly")
		}
	}()
	go s.gcLoop(ctx, logger)

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return s.httpServer.Shutdown(shutdownCtx)
}

// NeedLeaderElection is false because the chart runs a single operator replica.
func (s *Server) NeedLeaderElection() bool {
	return false
}

// Handler returns the HTTP handler, useful for tests.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/logs/", s.handle)
	return mux
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/v1/logs/")
	parts := strings.Split(path, "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		writeError(w, http.StatusBadRequest, "invalid log path")
		return
	}
	namespace, name := parts[0], parts[1]
	if err := validatePathPart(namespace); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := validatePathPart(name); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	switch {
	case len(parts) == 3 && parts[2] == "meta":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		s.handleMeta(w, r, namespace, name)
	case len(parts) == 3 && parts[2] == "finalize":
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		s.handleFinalize(w, r, namespace, name)
	case len(parts) == 2:
		switch r.Method {
		case http.MethodPut:
			s.handlePut(w, r, namespace, name)
		case http.MethodGet:
			s.handleGet(w, r, namespace, name)
		case http.MethodDelete:
			if err := s.config.Store.Reset(namespace, name); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		}
	default:
		writeError(w, http.StatusNotFound, "not found")
	}
}

func (s *Server) handlePut(w http.ResponseWriter, r *http.Request, namespace, name string) {
	offset, err := strconv.ParseInt(r.URL.Query().Get("offset"), 10, 64)
	if err != nil || offset < 0 {
		writeError(w, http.StatusBadRequest, "offset must be a non-negative integer")
		return
	}

	body := http.MaxBytesReader(w, r.Body, maxChunkBytes)
	data, err := io.ReadAll(body)
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "chunk is larger than 1MiB")
		return
	}
	if expected := r.Header.Get("X-Chunk-SHA256"); expected != "" {
		sum := sha256.Sum256(data)
		if !strings.EqualFold(expected, hex.EncodeToString(sum[:])) {
			writeError(w, http.StatusBadRequest, "chunk checksum mismatch")
			return
		}
	}

	if meta, metaErr := s.config.Store.Meta(namespace, name); metaErr == nil {
		if meta.Size+int64(len(data)) > s.config.MaxBuildBytes {
			writeError(w, http.StatusRequestEntityTooLarge, "build log exceeds the configured limit")
			return
		}
	} else if errors.Is(metaErr, os.ErrNotExist) {
		if int64(len(data)) > s.config.MaxBuildBytes {
			writeError(w, http.StatusRequestEntityTooLarge, "build log exceeds the configured limit")
			return
		}
	} else {
		writeError(w, http.StatusInternalServerError, metaErr.Error())
		return
	}

	size, err := s.config.Store.Append(namespace, name, offset, data)
	if err != nil {
		if errors.Is(err, ErrOffsetConflict) {
			writeJSON(w, http.StatusConflict, map[string]any{"size": size, "error": "offset conflict"})
			return
		}
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"size": size})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request, namespace, name string) {
	meta, err := s.config.Store.Meta(namespace, name)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, "log not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	if tailValue := r.URL.Query().Get("tail"); tailValue != "" {
		tail, parseErr := strconv.Atoi(tailValue)
		if parseErr != nil || tail < 0 {
			writeError(w, http.StatusBadRequest, "tail must be a non-negative integer")
			return
		}
		data, readErr := s.config.Store.ReadTail(namespace, name, tail)
		if readErr != nil {
			writeError(w, http.StatusInternalServerError, readErr.Error())
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("X-Log-Size", strconv.FormatInt(meta.Size, 10))
		w.Header().Set("X-Log-Complete", strconv.FormatBool(meta.Complete))
		_, _ = w.Write(data)
		return
	}

	file, err := s.config.Store.Open(namespace, name)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer func() { _ = file.Close() }()

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Log-Size", strconv.FormatInt(meta.Size, 10))
	w.Header().Set("X-Log-Complete", strconv.FormatBool(meta.Complete))

	offset, err := io.Copy(w, file)
	if err != nil {
		return
	}
	if r.URL.Query().Get("follow") != "true" {
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		return
	}
	flusher.Flush()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-time.After(500 * time.Millisecond):
		}

		stat, err := os.Stat(mustLogPath(s.config.Store, namespace, name))
		if err != nil {
			return
		}
		if stat.Size() > offset {
			if _, err := file.Seek(offset, io.SeekStart); err != nil {
				return
			}
			copied, err := io.Copy(w, file)
			if err != nil {
				return
			}
			offset += copied
			flusher.Flush()
			continue
		}

		meta, metaErr := s.config.Store.Meta(namespace, name)
		if metaErr == nil && meta.Complete {
			return
		}
	}
}

func (s *Server) handleMeta(w http.ResponseWriter, _ *http.Request, namespace, name string) {
	meta, err := s.config.Store.Meta(namespace, name)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, "log not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

func (s *Server) handleFinalize(w http.ResponseWriter, _ *http.Request, namespace, name string) {
	meta, err := s.config.Store.Finalize(namespace, name)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, "log not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

func (s *Server) gcLoop(ctx context.Context, logger interface {
	Info(string, ...any)
	Error(error, string, ...any)
}) {
	ticker := time.NewTicker(time.Hour)
	defer ticker.Stop()
	for {
		if _, err := s.config.Store.GC(s.config.Retention, s.config.MaxStoreBytes, s.config.Now()); err != nil {
			logger.Error(err, "log store GC failed")
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func mustLogPath(store *Store, namespace, name string) string {
	path, err := store.logPath(namespace, name)
	if err != nil {
		return ""
	}
	return path
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
