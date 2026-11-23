package config

import (
	"context"
	"log"
	"os"
	"time"
)

const watchInterval = 30 * time.Second

// WatchFile polls filename for changes and calls reload when the projected
// Secret file is replaced. Kubernetes Secret volumes swap a symlink; os.Stat
// follows it, so a new mtime triggers the reload. A failed reload keeps the
// current configuration.
func WatchFile(ctx context.Context, filename string, logger *log.Logger, reload func() error) {
	if filename == "" {
		return
	}
	ticker := time.NewTicker(watchInterval)
	defer ticker.Stop()

	var lastMod time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			fi, err := os.Stat(filename)
			if err != nil {
				continue
			}
			if mod := fi.ModTime(); mod.After(lastMod) {
				lastMod = mod
				if err := reload(); err != nil {
					logger.Printf("config %q reload failed: %v", filename, err)
				}
			}
		}
	}
}
