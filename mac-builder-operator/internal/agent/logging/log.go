package logging

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"k8s.io/apimachinery/pkg/util/validation"
)

var sensitiveTokenPattern = regexp.MustCompile(`(?i)(gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})`)

func redact(value string) string {
	return sensitiveTokenPattern.ReplaceAllString(value, "[REDACTED]")
}

// DefaultDir returns the per-user build log directory.
func DefaultDir() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("resolve user config dir: %w", err)
	}
	return filepath.Join(dir, "macbuild-agent", "logs"), nil
}

// Log is a per-build, size-capped, phase-tagged log file.
type Log struct {
	path      string
	maxBytes  int64
	mu        sync.Mutex
	file      *os.File
	written   int64
	truncated bool
	phase     string
	partial   []byte
}

// NewBuildLog creates a log file for a build.
func NewBuildLog(dir, namespace, name string, maxBytes int64) (*Log, error) {
	if errs := validation.IsDNS1123Subdomain(namespace); len(errs) > 0 {
		return nil, fmt.Errorf("invalid namespace %q: %v", namespace, errs)
	}
	if errs := validation.IsDNS1123Subdomain(name); len(errs) > 0 {
		return nil, fmt.Errorf("invalid build name %q: %v", name, errs)
	}
	if maxBytes <= 0 {
		maxBytes = 100 * 1024 * 1024
	}
	path := filepath.Join(dir, namespace, name+".log")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create log dir: %w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, fmt.Errorf("create log: %w", err)
	}
	return &Log{path: path, maxBytes: maxBytes, file: file, phase: "agent"}, nil
}

// Path returns the local log path.
func (l *Log) Path() string {
	return l.path
}

// Size returns the bytes written so far.
func (l *Log) Size() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.written
}

// SetPhase updates the phase used for subsequent command output.
func (l *Log) SetPhase(phase string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if phase == "" {
		return
	}
	l.phase = phase
}

// Phase writes a phase marker.
func (l *Log) Phase(phase, message string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if phase != "" {
		l.phase = phase
	}
	l.writeLineLocked(fmt.Sprintf("[%s] %s", phase, message))
}

// CommandWriter returns an io.Writer that captures command output with the
// current phase and a timestamp prefix.
func (l *Log) CommandWriter() io.Writer {
	return &commandWriter{log: l}
}

type commandWriter struct {
	log *Log
}

func (w *commandWriter) Write(p []byte) (int, error) {
	w.log.writeCommand(p)
	return len(p), nil
}

func (l *Log) writeCommand(p []byte) {
	l.mu.Lock()
	defer l.mu.Unlock()

	l.partial = append(l.partial, p...)
	for {
		idx := strings.IndexByte(string(l.partial), '\n')
		if idx < 0 {
			break
		}
		line := strings.TrimSuffix(string(l.partial[:idx]), "\r")
		l.writeLineLocked("[" + l.phase + "] " + line)
		l.partial = l.partial[idx+1:]
	}
}

func (l *Log) writeLineLocked(message string) {
	if l.file == nil {
		return
	}
	message = redact(message)
	if l.written >= l.maxBytes {
		if !l.truncated {
			l.truncated = true
			_, _ = fmt.Fprintf(l.file, "%s [agent] log truncated\n", time.Now().UTC().Format(time.RFC3339))
		}
		return
	}
	line := fmt.Sprintf("%s %s\n", time.Now().UTC().Format(time.RFC3339), message)
	remaining := l.maxBytes - l.written
	truncatedLine := false
	if int64(len(line)) > remaining {
		line = line[:remaining]
		truncatedLine = true
	}
	n, err := l.file.WriteString(line)
	if err != nil {
		return
	}
	l.written += int64(n)
	if truncatedLine && !l.truncated {
		l.truncated = true
		_, _ = fmt.Fprintf(l.file, "%s [agent] log truncated\n", time.Now().UTC().Format(time.RFC3339))
	}
}

// Close flushes a partial command line and closes the file.
func (l *Log) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return nil
	}
	if len(l.partial) > 0 {
		l.writeLineLocked("[" + l.phase + "] " + string(l.partial))
		l.partial = nil
	}
	err := l.file.Close()
	l.file = nil
	return err
}
