package config

import (
	"context"
	"fmt"
	"os"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// ReloadHandler is a callback invoked with the new config after a successful reload.
type ReloadHandler[T any] func(*T)

// Reloadable wraps config with thread-safe reload capability.
// It watches a YAML config file for changes and notifies registered handlers.
type Reloadable[T any] struct {
	mu       sync.RWMutex
	cfg      *T
	path     string
	mtime    time.Time
	handlers []ReloadHandler[T]
}

// NewReloadable reads the YAML config file and returns a Reloadable wrapper.
func NewReloadable[T any](path string) (*Reloadable[T], error) {
	cfg, fi, err := loadFileGeneric[T](path)
	if err != nil {
		return nil, fmt.Errorf("load config: %w", err)
	}
	return &Reloadable[T]{
		cfg:   cfg,
		path:  path,
		mtime: fi.ModTime(),
	}, nil
}

// Get returns the current config in a thread-safe manner.
func (r *Reloadable[T]) Get() *T {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.cfg
}

// Reload re-reads the config file from disk and calls all registered handlers
// with the new config. It is safe to call concurrently.
func (r *Reloadable[T]) Reload() error {
	cfg, fi, err := loadFileGeneric[T](r.path)
	if err != nil {
		return fmt.Errorf("reload config: %w", err)
	}

	r.mu.Lock()
	r.cfg = cfg
	r.mtime = fi.ModTime()
	handlers := make([]ReloadHandler[T], len(r.handlers))
	copy(handlers, r.handlers)
	r.mu.Unlock()

	for _, h := range handlers {
		h(cfg)
	}
	return nil
}

// OnReload registers a handler that is called after each successful reload.
// Handlers are called in registration order.
func (r *Reloadable[T]) OnReload(handler ReloadHandler[T]) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.handlers = append(r.handlers, handler)
}

// AutoReload polls the config file for modification changes at the given
// interval and triggers a reload when a change is detected. It blocks until
// the context is cancelled.
func (r *Reloadable[T]) AutoReload(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		fi, err := os.Stat(r.path)
		if err != nil {
			continue
		}

		r.mu.RLock()
		old := r.mtime
		r.mu.RUnlock()

		if fi.ModTime().After(old) {
			if err := r.Reload(); err != nil {
				fmt.Fprintf(os.Stderr, "config auto-reload error: %v\n", err)
			}
		}
	}
}

func loadFileGeneric[T any](path string) (*T, os.FileInfo, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	var cfg T
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, nil, err
	}
	fi, err := os.Stat(path)
	if err != nil {
		return nil, nil, err
	}
	return &cfg, fi, nil
}
