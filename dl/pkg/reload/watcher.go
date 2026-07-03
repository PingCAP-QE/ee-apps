package reload

import (
	"context"
	"log"
	"os"
	"time"
)

const interval = 30 * time.Second

// WatchFile polls filePath for modification. When the file changes (or appears
// after not existing), it calls reloadFn. Runs until ctx is cancelled.
func WatchFile(ctx context.Context, filePath string, logger *log.Logger, reloadFn func() error) {
	if filePath == "" {
		return
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	var lastMod time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			fi, err := os.Stat(filePath)
			if err != nil {
				continue
			}
			modTime := fi.ModTime()
			if modTime.After(lastMod) {
				lastMod = modTime
				logger.Printf("Config %q changed, reloading...", filePath)
				if err := reloadFn(); err != nil {
					logger.Printf("Reload of %q failed: %v", filePath, err)
				}
			}
		}
	}
}
