package gitcred

import (
	"io"
	"log/slog"
)

// NewLogger returns a logger that writes warnings to w. The helper never logs
// token material, so any writer is safe to pass.
func NewLogger(w io.Writer) *slog.Logger {
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelWarn}))
}
