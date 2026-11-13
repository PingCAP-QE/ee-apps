package kafka

import (
	"os"
	"strings"
	"testing"
)

func TestTLSConfigWithoutCAFile(t *testing.T) {
	cfg, err := Authentication{}.tlsConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.RootCAs != nil {
		t.Fatal("expected system roots when no CA file is configured")
	}
}

func TestTLSConfigWithMissingCAFile(t *testing.T) {
	_, err := Authentication{CAFile: "/nonexistent/ca.crt"}.tlsConfig()
	if err == nil || !strings.Contains(err.Error(), "reading kafka CA file") {
		t.Fatalf("expected a read error naming the file, got %v", err)
	}
}

func TestTLSConfigWithInvalidPEM(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/ca.crt"
	if err := writeFile(path, "not a certificate"); err != nil {
		t.Fatal(err)
	}
	_, err := Authentication{CAFile: path}.tlsConfig()
	if err == nil || !strings.Contains(err.Error(), "no certificates found") {
		t.Fatalf("expected a parse error, got %v", err)
	}
}

func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}
