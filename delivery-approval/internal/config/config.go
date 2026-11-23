// Package config loads and hot-reloads the approval service configuration.
package config

import (
	"errors"
	"fmt"
	"net/mail"
	"net/url"
	"os"
	"strings"
	"sync"

	"gopkg.in/yaml.v3"
)

const (
	DefaultListen        = ":8080"
	DefaultHealthAddress = ":8081"
	DefaultPublicURL     = "https://prow.tidb.net/dl/grants"
	DefaultDLBaseURL     = "http://dl-management.dl.svc.cluster.local"
	MaxTTLSeconds        = int64(7 * 24 * 60 * 60)
)

// Config is the runtime configuration. Secrets (database DSN, Lark app secret,
// URL encryption key, dl issuer secret) are read from the environment/Secret
// and are deliberately absent here.
type Config struct {
	Listen        string `yaml:"listen"`
	HealthAddress string `yaml:"health_address"`
	// PublicURL is the browser-facing base of the approval pages. It is used to
	// build the notification card and approval links.
	PublicURL string `yaml:"public_url"`
	// DLBaseURL is the in-cluster dl-management origin.
	DLBaseURL string `yaml:"dl_base_url"`

	AllowedEmailDomains []string `yaml:"allowed_email_domains"`
	ApproverEmails      []string `yaml:"approver_emails"`

	MaxPendingPerRequester int `yaml:"max_pending_per_requester"`
	MaxRequestsPerDay      int `yaml:"max_requests_per_day"`

	DefaultTTLSeconds int64 `yaml:"default_ttl_seconds"`
	MaxTTLSeconds     int64 `yaml:"max_ttl_seconds"`
}

// Default returns the configuration used before the YAML file is applied.
func Default() *Config {
	return &Config{
		Listen:                 DefaultListen,
		HealthAddress:          DefaultHealthAddress,
		PublicURL:              DefaultPublicURL,
		DLBaseURL:              DefaultDLBaseURL,
		AllowedEmailDomains:    []string{"pingcap.com"},
		MaxPendingPerRequester: 5,
		MaxRequestsPerDay:      20,
		DefaultTTLSeconds:      24 * 60 * 60,
		MaxTTLSeconds:          MaxTTLSeconds,
	}
}

// LoadConfig reads and validates the YAML file. Unknown fields are rejected so
// a typo cannot silently disable a limit.
func LoadConfig(filename string) (*Config, error) {
	f, err := os.Open(filename)
	if err != nil {
		return nil, errors.New("cannot read approval config")
	}
	defer f.Close()

	cfg := Default()
	d := yaml.NewDecoder(f)
	d.KnownFields(true)
	if err := d.Decode(cfg); err != nil {
		return nil, errors.New("invalid approval config")
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

func (c *Config) Validate() error {
	if strings.TrimSpace(c.Listen) == "" || strings.TrimSpace(c.HealthAddress) == "" {
		return errors.New("approval listen addresses are required")
	}
	if err := validateBaseURL(c.PublicURL, true); err != nil {
		return fmt.Errorf("approval public_url: %w", err)
	}
	c.PublicURL = strings.TrimSuffix(c.PublicURL, "/")
	if err := validateBaseURL(c.DLBaseURL, false); err != nil {
		return fmt.Errorf("approval dl_base_url: %w", err)
	}
	c.DLBaseURL = strings.TrimSuffix(c.DLBaseURL, "/")

	if len(c.AllowedEmailDomains) == 0 {
		return errors.New("approval requires allowed_email_domains")
	}
	domains := make([]string, 0, len(c.AllowedEmailDomains))
	seenDomains := map[string]bool{}
	for _, raw := range c.AllowedEmailDomains {
		d := strings.ToLower(strings.TrimSpace(raw))
		if d == "" || strings.ContainsAny(d, "@ /\t\r\n") || !strings.Contains(d, ".") {
			return errors.New("invalid approval email domain")
		}
		if seenDomains[d] {
			continue
		}
		seenDomains[d] = true
		domains = append(domains, d)
	}
	c.AllowedEmailDomains = domains

	if len(c.ApproverEmails) == 0 {
		return errors.New("approval requires approver_emails")
	}
	approvers := make([]string, 0, len(c.ApproverEmails))
	seenApprovers := map[string]bool{}
	for _, raw := range c.ApproverEmails {
		trimmed := strings.TrimSpace(raw)
		addr, err := mail.ParseAddress(trimmed)
		if err != nil || addr.Address != trimmed {
			return errors.New("invalid approver email")
		}
		email := strings.ToLower(addr.Address)
		if seenApprovers[email] {
			continue
		}
		seenApprovers[email] = true
		approvers = append(approvers, email)
	}
	c.ApproverEmails = approvers

	if c.MaxPendingPerRequester < 1 || c.MaxRequestsPerDay < 1 {
		return errors.New("approval request limits must be positive")
	}
	if c.DefaultTTLSeconds < 1 || c.MaxTTLSeconds < 1 || c.DefaultTTLSeconds > c.MaxTTLSeconds || c.MaxTTLSeconds > MaxTTLSeconds {
		return errors.New("invalid approval TTL limits")
	}
	return nil
}

func validateBaseURL(raw string, requireHTTPS bool) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("must be an absolute URL without credentials, query or fragment")
	}
	if requireHTTPS && u.Scheme != "https" {
		return errors.New("must use HTTPS")
	}
	if !requireHTTPS && u.Scheme != "http" && u.Scheme != "https" {
		return errors.New("must use HTTP or HTTPS")
	}
	if u.Path != "" && u.Path != "/" {
		// A path on the dl base would make the share endpoint ambiguous; the
		// public URL may have a path prefix (/dl/grants).
		if !requireHTTPS {
			return errors.New("must not contain a path")
		}
	}
	return nil
}

// Manager guards the immutable *Config. Reload swaps the pointer; in-flight
// callers keep the config they started with.
type Manager struct {
	mu  sync.RWMutex
	cfg *Config
}

func NewManager(cfg *Config) *Manager {
	return &Manager{cfg: cfg}
}

func (m *Manager) Current() *Config {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.cfg
}

// Reload validates the file and swaps it in. An invalid document leaves the
// running configuration untouched.
func (m *Manager) Reload(filename string) error {
	cfg, err := LoadConfig(filename)
	if err != nil {
		return err
	}
	m.mu.Lock()
	m.cfg = cfg
	m.mu.Unlock()
	return nil
}

// IsApprover reports whether email is in the current allowlist. The check is
// case-insensitive; config validation stores lowercase addresses.
func (m *Manager) IsApprover(email string) bool {
	email = strings.ToLower(strings.TrimSpace(email))
	for _, candidate := range m.Current().ApproverEmails {
		if candidate == email {
			return true
		}
	}
	return false
}
