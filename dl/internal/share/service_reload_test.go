package share

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func writePolicyFile(t *testing.T, path, secret, origin string) {
	t.Helper()
	sum := sha256.Sum256([]byte(secret))
	body := "public_url: " + origin + "\nissuers:\n" +
		"  - id: release-service\n" +
		"    token_sha256: \"" + hex.EncodeToString(sum[:]) + "\"\n" +
		"    repositories:\n      - registry.example.com/releases/\n" +
		"    allowed_tags: 'v[0-9]+\\.[0-9]+\\.[0-9]+'\n" +
		"    max_ttl_seconds: 86400\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestReloadConfigSwapsPolicy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.yaml")
	writePolicyFile(t, path, "first-secret", "https://first.example.com")
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	s := &Service{Config: cfg}
	if s.Policy().Authenticate("first-secret") == nil {
		t.Fatal("initial policy not loaded")
	}

	writePolicyFile(t, path, "second-secret", "https://second.example.com")
	if err := s.ReloadConfig(path); err != nil {
		t.Fatal(err)
	}
	if s.Policy().Authenticate("first-secret") != nil {
		t.Fatal("retired credential still authenticates")
	}
	if s.Policy().Authenticate("second-secret") == nil {
		t.Fatal("rotated credential does not authenticate")
	}
	if got := s.Policy().PublicURL; got != "https://second.example.com" {
		t.Fatalf("public url = %q", got)
	}
}

func TestReloadConfigKeepsRunningPolicyOnInvalidDocument(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.yaml")
	writePolicyFile(t, path, "keep-me", "https://keep.example.com")
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	s := &Service{Config: cfg}

	if err := os.WriteFile(path, []byte("public_url: http://insecure\nissuers: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := s.ReloadConfig(path); err == nil {
		t.Fatal("expected invalid policy to be rejected")
	}
	if s.Policy().Authenticate("keep-me") == nil {
		t.Fatal("running policy was replaced by an invalid document")
	}
}

func TestReloadConfigConcurrentReads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.yaml")
	writePolicyFile(t, path, "concurrent-secret", "https://concurrent.example.com")
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	s := &Service{Config: cfg}

	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				if p := s.Policy(); p == nil || p.PublicURL == "" {
					t.Error("policy read returned nil")
					return
				}
			}
		}()
	}
	for i := 0; i < 20; i++ {
		if err := s.ReloadConfig(path); err != nil {
			t.Fatal(err)
		}
	}
	wg.Wait()
}
