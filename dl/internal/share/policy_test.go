package share

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

func policyConfig() *Config {
	h := sha256.Sum256([]byte("test-issuer-secret"))
	return &Config{PublicURL: "https://downloads.example.com", Issuers: []Policy{{ID: "release-service", TokenSHA256: hex.EncodeToString(h[:]), Repositories: []string{"registry.example.com/releases/", "registry.example.com/exact"}, AllowedTags: `v[0-9]+\.[0-9]+\.[0-9]+`, MaxTTLSeconds: 86400}}}
}

func TestPolicyBoundaries(t *testing.T) {
	c := policyConfig()
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	p := c.Authenticate("test-issuer-secret")
	if p == nil || c.Authenticate("wrong") != nil {
		t.Fatal("authentication failed")
	}
	for _, tt := range []struct {
		repo, tag string
		want      bool
	}{
		{"registry.example.com/releases/tidb", "v8.1.0", true},
		{"registry.example.com/exact", "v8.1.0", true},
		{"registry.example.com/exact/child", "v8.1.0", false},
		{"registry.example.com/releases-evil/tidb", "v8.1.0", false},
		{"evil.example.com/releases/tidb", "v8.1.0", false},
		{"registry.example.com/releases/tidb:latest", "v8.1.0", false},
		{"registry.example.com/releases/../private", "v8.1.0", false},
		{"registry.example.com/releases/tidb", "latest", false},
		{"registry.example.com/releases/tidb", "v8.1.0-extra", false},
		{"registry.example.com/releases/tidb", "v8.1.0\n", false},
	} {
		t.Run(tt.repo+tt.tag, func(t *testing.T) {
			if got := p.Allows(tt.repo, tt.tag); got != tt.want {
				t.Fatalf("got %v, want %v", got, tt.want)
			}
		})
	}
}

func TestInvalidPoliciesFailClosed(t *testing.T) {
	for _, mutate := range []func(*Config){
		func(c *Config) { c.PublicURL = "http://downloads.example.com" },
		func(c *Config) { c.PublicURL = "https://downloads.example.com/path" },
		func(c *Config) { c.PublicURL = "https://downloads.example.com?" },
		func(c *Config) { c.PublicURL = "https://downloads.example.com#" },
		func(c *Config) { c.Issuers = nil },
		func(c *Config) { c.Issuers[0].TokenSHA256 = "bad" },
		func(c *Config) { c.Issuers[0].Repositories = []string{"registry.example.com/releases:tag"} },
		func(c *Config) { c.Issuers[0].AllowedTags = "[" },
		func(c *Config) { c.Issuers[0].MaxTTLSeconds = 31 * 86400 },
		func(c *Config) { c.Issuers = append(c.Issuers, c.Issuers[0]) },
	} {
		c := policyConfig()
		mutate(c)
		if c.Validate() == nil {
			t.Fatal("invalid policy accepted")
		}
	}
}

func TestFilenameHeaders(t *testing.T) {
	for _, s := range []string{"../secret", "path/file", "path\\file", "a\r\nX-Injected: yes", "a\x00", "..", ""} {
		if validFilename(s) {
			t.Fatalf("unsafe filename accepted: %q", s)
		}
	}
	for _, s := range []string{"tidb.tar.gz", `a"b.tar.gz`, "数据.tar.gz"} {
		if !validFilename(s) {
			t.Fatalf("safe filename rejected: %q", s)
		}
	}
}
