package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRun(t *testing.T) {
	valid, err := os.ReadFile("../../policies/registry-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	validFile := filepath.Join(dir, "valid.json")
	if err := os.WriteFile(validFile, valid, 0o600); err != nil {
		t.Fatal(err)
	}
	invalidFile := filepath.Join(dir, "invalid.json")
	if err := os.WriteFile(invalidFile, []byte(`{"version":"","routes":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name     string
		args     []string
		wantCode int
		wantOut  string
		wantErr  string
	}{
		{"valid report", []string{validFile}, 0, "tibuild-devbuild-rerun", "rules with resource checks: 0"},
		{"invalid registry", []string{invalidFile}, 1, "", "registry version is required"},
		{"missing file", []string{filepath.Join(dir, "missing.json")}, 2, "", "no such file"},
		{"usage", nil, 2, "", "usage: registry-check"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := run(tt.args, &stdout, &stderr); code != tt.wantCode {
				t.Fatalf("exit = %d, want %d", code, tt.wantCode)
			}
			if !strings.Contains(stdout.String(), tt.wantOut) {
				t.Fatalf("stdout %q does not contain %q", stdout.String(), tt.wantOut)
			}
			if !strings.Contains(stderr.String(), tt.wantErr) {
				t.Fatalf("stderr %q does not contain %q", stderr.String(), tt.wantErr)
			}
		})
	}
}
