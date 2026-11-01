package logging

import (
	"os"
	"strings"
	"testing"
)

func TestBuildLogWritesPhasesAndCommandOutput(t *testing.T) {
	t.Parallel()

	log, err := NewBuildLog(t.TempDir(), "ee-cd", "macbuild-abc", 1024*1024)
	if err != nil {
		t.Fatalf("new log: %v", err)
	}
	log.SetPhase("Building")
	log.Phase("Building", "start build")
	if _, err := log.CommandWriter().Write([]byte("line one\nline two\n")); err != nil {
		t.Fatalf("write command: %v", err)
	}
	if err := log.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	data, err := os.ReadFile(log.Path())
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	content := string(data)
	for _, want := range []string{"[Building] start build", "[Building] line one", "[Building] line two"} {
		if !strings.Contains(content, " "+want) {
			t.Fatalf("log does not contain %q:\n%s", want, content)
		}
	}
	if log.Size() == 0 {
		t.Fatal("expected a non-zero log size")
	}
}

func TestBuildLogTruncates(t *testing.T) {
	t.Parallel()

	log, err := NewBuildLog(t.TempDir(), "ee-cd", "macbuild-abc", 80)
	if err != nil {
		t.Fatalf("new log: %v", err)
	}
	if _, err := log.CommandWriter().Write([]byte(strings.Repeat("x", 500) + "\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := log.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	data, err := os.ReadFile(log.Path())
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if !strings.Contains(string(data), "log truncated") {
		t.Fatalf("expected truncation marker:\n%s", data)
	}
}

func TestBuildLogRejectsInvalidNames(t *testing.T) {
	t.Parallel()

	if _, err := NewBuildLog(t.TempDir(), "../escape", "macbuild-abc", 1024); err == nil {
		t.Fatal("expected an invalid namespace error")
	}
	if _, err := NewBuildLog(t.TempDir(), "ee-cd", "bad/name", 1024); err == nil {
		t.Fatal("expected an invalid build name error")
	}
}

func TestBuildLogRedactsTokens(t *testing.T) {
	t.Parallel()

	log, err := NewBuildLog(t.TempDir(), "ee-cd", "macbuild-abc", 1024*1024)
	if err != nil {
		t.Fatalf("new log: %v", err)
	}
	token := "ghp_" + strings.Repeat("a", 30)
	log.Phase("Building", "token="+token)
	if err := log.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	data, err := os.ReadFile(log.Path())
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if strings.Contains(string(data), token) {
		t.Fatalf("token was written to the log:\n%s", data)
	}
	if !strings.Contains(string(data), "[REDACTED]") {
		t.Fatalf("expected redaction marker:\n%s", data)
	}
}

func TestBuildLogRedactsPartialLineOnClose(t *testing.T) {
	t.Parallel()

	log, err := NewBuildLog(t.TempDir(), "ee-cd", "macbuild-abc", 1024*1024)
	if err != nil {
		t.Fatalf("new log: %v", err)
	}
	token := "ghp_" + strings.Repeat("b", 30)
	if _, err := log.CommandWriter().Write([]byte("token=" + token)); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := log.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	data, err := os.ReadFile(log.Path())
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if strings.Contains(string(data), token) {
		t.Fatalf("partial line bypassed redaction:\n%s", data)
	}
	if !strings.Contains(string(data), "[REDACTED]") {
		t.Fatalf("expected redaction marker:\n%s", data)
	}
}
