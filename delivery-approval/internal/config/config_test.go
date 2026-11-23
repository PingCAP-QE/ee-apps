package config

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func validConfig() *Config {
	return &Config{
		Listen:                 ":8080",
		HealthAddress:          ":8081",
		PublicURL:              "https://approvals.example.com",
		DLBaseURL:              "http://dl-management:80",
		AllowedEmailDomains:    []string{"example.com"},
		ApproverEmails:         []string{"approver@example.com"},
		MaxPendingPerRequester: 5,
		MaxRequestsPerDay:      20,
		DefaultTTLSeconds:      86400,
		MaxTTLSeconds:          604800,
	}
}

func TestDefaultsAndValidation(t *testing.T) {
	cfg := Default()
	if cfg.Listen != DefaultListen || cfg.HealthAddress != DefaultHealthAddress {
		t.Fatalf("unexpected listen defaults: %+v", cfg)
	}
	if cfg.DefaultTTLSeconds != 86400 || cfg.MaxTTLSeconds != MaxTTLSeconds {
		t.Fatalf("unexpected TTL defaults: %+v", cfg)
	}
	if err := cfg.Validate(); err == nil {
		t.Fatal("default config without approvers must be rejected")
	}

	cfg = validConfig()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}
}

func TestLoadConfigNormalizes(t *testing.T) {
	path := writeConfig(t, `
listen: ":8080"
health_address: ":8081"
public_url: https://approvals.example.com/
dl_base_url: http://dl-management:80/
allowed_email_domains: [Example.com, example.com]
approver_emails: [Approver@Example.com, approver@example.com, second@example.com]
max_pending_per_requester: 3
max_requests_per_day: 10
default_ttl_seconds: 3600
max_ttl_seconds: 7200
`)
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PublicURL != "https://approvals.example.com" {
		t.Fatalf("public url not normalized: %q", cfg.PublicURL)
	}
	if cfg.DLBaseURL != "http://dl-management:80" {
		t.Fatalf("dl base not normalized: %q", cfg.DLBaseURL)
	}
	if len(cfg.AllowedEmailDomains) != 1 || cfg.AllowedEmailDomains[0] != "example.com" {
		t.Fatalf("domains not deduplicated: %v", cfg.AllowedEmailDomains)
	}
	if len(cfg.ApproverEmails) != 2 || cfg.ApproverEmails[0] != "approver@example.com" {
		t.Fatalf("approvers not normalized: %v", cfg.ApproverEmails)
	}
}

func TestInvalidConfigs(t *testing.T) {
	mutations := map[string]func(*Config){
		"http public url":     func(c *Config) { c.PublicURL = "http://approvals.example.com" },
		"public query":        func(c *Config) { c.PublicURL = "https://approvals.example.com?x=1" },
		"dl base path":        func(c *Config) { c.DLBaseURL = "http://dl-management/api" },
		"empty domains":       func(c *Config) { c.AllowedEmailDomains = nil },
		"bad domain":          func(c *Config) { c.AllowedEmailDomains = []string{"example"} },
		"empty approvers":     func(c *Config) { c.ApproverEmails = nil },
		"bad approver":        func(c *Config) { c.ApproverEmails = []string{"not-an-email"} },
		"display name":        func(c *Config) { c.ApproverEmails = []string{"A <a@example.com>"} },
		"zero pending limit":  func(c *Config) { c.MaxPendingPerRequester = 0 },
		"zero daily limit":    func(c *Config) { c.MaxRequestsPerDay = 0 },
		"default ttl zero":    func(c *Config) { c.DefaultTTLSeconds = 0 },
		"default over max":    func(c *Config) { c.DefaultTTLSeconds = 7201; c.MaxTTLSeconds = 7200 },
		"max over 7d":         func(c *Config) { c.MaxTTLSeconds = MaxTTLSeconds + 1; c.DefaultTTLSeconds = 3600 },
		"empty listen":        func(c *Config) { c.Listen = "" },
		"empty health listen": func(c *Config) { c.HealthAddress = "" },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			cfg := validConfig()
			mutate(cfg)
			if err := cfg.Validate(); err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	path := writeConfig(t, `
public_url: https://approvals.example.com
dl_base_url: http://dl-management:80
allowed_email_domains: [example.com]
approver_emails: [approver@example.com]
max_pending_per_requester: 5
max_requests_per_day: 20
default_ttl_seconds: 3600
max_ttl_seconds: 7200
typo_field: true
`)
	if _, err := LoadConfig(path); err == nil {
		t.Fatal("unknown field accepted")
	}
}

func TestReloadSwapsAndKeepsOnError(t *testing.T) {
	path := writeConfig(t, `
public_url: https://approvals.example.com
dl_base_url: http://dl-management:80
allowed_email_domains: [example.com]
approver_emails: [first@example.com]
max_pending_per_requester: 5
max_requests_per_day: 20
default_ttl_seconds: 3600
max_ttl_seconds: 7200
`)
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	m := NewManager(cfg)
	if !m.IsApprover("first@example.com") {
		t.Fatal("initial approver not loaded")
	}

	if err := os.WriteFile(path, []byte("public_url: http://insecure\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := m.Reload(path); err == nil {
		t.Fatal("invalid reload accepted")
	}
	if !m.IsApprover("first@example.com") {
		t.Fatal("invalid reload replaced the running config")
	}

	writeConfig := `
public_url: https://approvals.example.com
dl_base_url: http://dl-management:80
allowed_email_domains: [example.com]
approver_emails: [second@example.com]
max_pending_per_requester: 5
max_requests_per_day: 20
default_ttl_seconds: 3600
max_ttl_seconds: 7200
`
	if err := os.WriteFile(path, []byte(writeConfig), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := m.Reload(path); err != nil {
		t.Fatal(err)
	}
	if m.IsApprover("first@example.com") || !m.IsApprover("second@example.com") {
		t.Fatal("reload did not swap the approver list")
	}
}

func TestConcurrentReadsDuringReload(t *testing.T) {
	path := writeConfig(t, `
public_url: https://approvals.example.com
dl_base_url: http://dl-management:80
allowed_email_domains: [example.com]
approver_emails: [approver@example.com]
max_pending_per_requester: 5
max_requests_per_day: 20
default_ttl_seconds: 3600
max_ttl_seconds: 7200
`)
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	m := NewManager(cfg)

	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				if c := m.Current(); c == nil || c.PublicURL == "" {
					t.Error("nil config during reload")
					return
				}
			}
		}()
	}
	for i := 0; i < 20; i++ {
		if err := m.Reload(path); err != nil {
			t.Fatal(err)
		}
	}
	wg.Wait()
}
