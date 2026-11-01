package workspace

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/go-logr/logr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

// Cleanup removes directories under root whose name starts with prefix and
// whose modification time is older than maxAge. It returns the number removed.
func Cleanup(root, prefix string, maxAge time.Duration, now time.Time) (int, error) {
	if strings.TrimSpace(prefix) == "" {
		return 0, fmt.Errorf("workspace prefix must not be empty")
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return 0, fmt.Errorf("read workspace root: %w", err)
	}

	removed := 0
	var errs []error
	for _, entry := range entries {
		if !entry.IsDir() || !strings.HasPrefix(entry.Name(), prefix) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			errs = append(errs, fmt.Errorf("stat %s: %w", entry.Name(), err))
			continue
		}
		if now.Sub(info.ModTime()) < maxAge {
			continue
		}
		if err := os.RemoveAll(filepath.Join(root, entry.Name())); err != nil {
			errs = append(errs, fmt.Errorf("remove %s: %w", entry.Name(), err))
			continue
		}
		removed++
	}
	return removed, errors.Join(errs...)
}

// Cleaner periodically removes stale build workspaces.
type Cleaner struct {
	Root     string
	Prefix   string
	MaxAge   time.Duration
	Interval time.Duration
	Now      func() time.Time
}

// Start runs one cleanup immediately and then on the configured interval.
func (c *Cleaner) Start(ctx context.Context) error {
	logger := logf.FromContext(ctx).WithName("workspace-gc")
	c.run(logger)

	interval := c.Interval
	if interval <= 0 {
		interval = time.Hour
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			c.run(logger)
		}
	}
}

// NeedLeaderElection is false: each worker cleans its own temp directory.
func (c *Cleaner) NeedLeaderElection() bool {
	return false
}

func (c *Cleaner) run(logger logr.Logger) {
	now := time.Now()
	if c.Now != nil {
		now = c.Now()
	}
	maxAge := c.MaxAge
	if maxAge <= 0 {
		maxAge = 7 * 24 * time.Hour
	}
	removed, err := Cleanup(c.Root, c.Prefix, maxAge, now)
	if removed > 0 {
		logger.Info("Removed stale build workspaces", "count", removed, "root", c.Root)
	}
	if err != nil {
		logger.Info("Some build workspaces could not be removed", "error", err.Error())
	}
}
