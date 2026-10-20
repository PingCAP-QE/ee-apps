package config

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
)

// Config is the broker configuration, normally mounted from a ConfigMap.
type Config struct {
	Server    Server       `yaml:"server"`
	GitHub    GitHub       `yaml:"github"`
	Cache     Cache        `yaml:"cache"`
	Auth      Auth         `yaml:"auth"`
	RateLimit RateLimit    `yaml:"rate_limit"`
	Reload    Reload       `yaml:"reload"`
	Policy    PolicyConfig `yaml:"policy"`

	policy *authz.Policy
}

// Server configures the HTTP listener.
type Server struct {
	Address         string        `yaml:"address"`
	ReadTimeout     time.Duration `yaml:"read_timeout"`
	WriteTimeout    time.Duration `yaml:"write_timeout"`
	ShutdownTimeout time.Duration `yaml:"shutdown_timeout"`
}

// GitHub configures the GitHub App and the optional fallback PAT.
type GitHub struct {
	AppID           int64         `yaml:"app_id"`
	PrivateKeyPath  string        `yaml:"private_key_path"`
	APIURL          string        `yaml:"api_url"`
	RequestTimeout  time.Duration `yaml:"request_timeout"`
	FallbackPAT     string        `yaml:"fallback_pat"`
	FallbackPATFile string        `yaml:"fallback_pat_file"`
}

// Cache configures the in-memory installation token cache.
type Cache struct {
	TTLMargin  time.Duration `yaml:"ttl_margin"`
	MaxEntries int           `yaml:"max_entries"`
}

// Auth configures how the broker authenticates callers through TokenReview.
type Auth struct {
	// KubernetesAPIURL overrides in-cluster discovery (useful for tests).
	KubernetesAPIURL string        `yaml:"kubernetes_api_url"`
	TokenFile        string        `yaml:"token_file"`
	CAFile           string        `yaml:"ca_file"`
	RequestTimeout   time.Duration `yaml:"request_timeout"`
}

// RateLimit configures the per-caller request limit. A zero value disables it.
type RateLimit struct {
	PerMinute int `yaml:"per_minute"`
}

// Reload configures periodic configuration reloading.
type Reload struct {
	Disabled bool          `yaml:"disabled"`
	Interval time.Duration `yaml:"interval"`
}

// PolicyConfig selects where the authorization policy is loaded from.
// When File is set it takes precedence over Rules; mount it from a Secret or
// ConfigMap when the policy must not live in the GitOps repository.
type PolicyConfig struct {
	File  string       `yaml:"file"`
	Rules []authz.Rule `yaml:"rules"`
}

// Load reads, defaults, and validates the configuration file. When
// policy.file is set, that file is read and validated as well.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	var cfg Config
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if err := decoder.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("decode config: %w", err)
	}
	cfg.applyDefaults()
	if err := cfg.preparePolicy(path); err != nil {
		return nil, err
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func (c *Config) applyDefaults() {
	if c.Server.Address == "" {
		c.Server.Address = ":8080"
	}
	if c.Server.ReadTimeout == 0 {
		c.Server.ReadTimeout = 10 * time.Second
	}
	if c.Server.WriteTimeout == 0 {
		c.Server.WriteTimeout = 30 * time.Second
	}
	if c.Server.ShutdownTimeout == 0 {
		c.Server.ShutdownTimeout = 10 * time.Second
	}
	if c.GitHub.APIURL == "" {
		c.GitHub.APIURL = "https://api.github.com"
	}
	if c.GitHub.RequestTimeout == 0 {
		c.GitHub.RequestTimeout = 30 * time.Second
	}
	if c.Cache.TTLMargin == 0 {
		c.Cache.TTLMargin = 5 * time.Minute
	}
	if c.Cache.MaxEntries == 0 {
		c.Cache.MaxEntries = 1000
	}
	if c.Auth.TokenFile == "" {
		c.Auth.TokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	}
	if c.Auth.CAFile == "" {
		c.Auth.CAFile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	}
	if c.Auth.RequestTimeout == 0 {
		c.Auth.RequestTimeout = 10 * time.Second
	}
	if c.RateLimit.PerMinute == 0 {
		c.RateLimit.PerMinute = 120
	}
	if c.Reload.Interval == 0 {
		c.Reload.Interval = 5 * time.Second
	}
}

// Validate checks required fields. The policy is validated by preparePolicy.
func (c *Config) Validate() error {
	if strings.TrimSpace(c.Server.Address) == "" {
		return fmt.Errorf("server.address is required")
	}
	if c.GitHub.AppID <= 0 {
		return fmt.Errorf("github.app_id must be positive")
	}
	if strings.TrimSpace(c.GitHub.PrivateKeyPath) == "" {
		return fmt.Errorf("github.private_key_path is required")
	}
	if c.Cache.TTLMargin <= 0 {
		return fmt.Errorf("cache.ttl_margin must be positive")
	}
	if c.Cache.MaxEntries <= 0 {
		return fmt.Errorf("cache.max_entries must be positive")
	}
	if c.RateLimit.PerMinute < 0 {
		return fmt.Errorf("rate_limit.per_minute must not be negative")
	}
	if !c.Reload.Disabled && c.Reload.Interval <= 0 {
		return fmt.Errorf("reload.interval must be positive")
	}
	if c.GitHub.FallbackPAT == "" && c.GitHub.FallbackPATFile != "" {
		data, err := os.ReadFile(c.GitHub.FallbackPATFile)
		if err != nil {
			return fmt.Errorf("read github.fallback_pat_file: %w", err)
		}
		c.GitHub.FallbackPAT = strings.TrimSpace(string(data))
	}
	return nil
}

func (c *Config) preparePolicy(configPath string) error {
	if c.Policy.File != "" {
		if !filepath.IsAbs(c.Policy.File) {
			c.Policy.File = filepath.Join(filepath.Dir(configPath), c.Policy.File)
		}
		data, err := os.ReadFile(c.Policy.File)
		if err != nil {
			return fmt.Errorf("read policy file: %w", err)
		}
		var policy authz.Policy
		decoder := yaml.NewDecoder(bytes.NewReader(data))
		decoder.KnownFields(true)
		if err := decoder.Decode(&policy); err != nil {
			return fmt.Errorf("decode policy file: %w", err)
		}
		if err := policy.Validate(); err != nil {
			return fmt.Errorf("policy: %w", err)
		}
		c.policy = &policy
		return nil
	}
	policy := &authz.Policy{Rules: c.Policy.Rules}
	if err := policy.Validate(); err != nil {
		return fmt.Errorf("policy: %w", err)
	}
	c.policy = policy
	return nil
}

// EffectivePolicy returns the resolved policy.
func (c *Config) EffectivePolicy() *authz.Policy {
	return c.policy
}

// HasFallback reports whether a fallback PAT is configured.
func (c *Config) HasFallback() bool {
	return c.GitHub.FallbackPAT != ""
}

// Provider supplies the current configuration.
type Provider interface {
	Current() *Config
}

// StaticProvider is a Provider with a fixed configuration (useful in tests).
type StaticProvider struct {
	Cfg *Config
}

// Current returns the fixed configuration.
func (p StaticProvider) Current() *Config {
	return p.Cfg
}

// Store keeps an atomically replaceable configuration and reloads it when the
// configuration or policy file changes on disk.
type Store struct {
	path    string
	logger  *slog.Logger
	current atomic.Pointer[Config]
}

// NewStore loads the initial configuration and returns a store for it.
func NewStore(path string, logger *slog.Logger) (*Store, error) {
	if logger == nil {
		logger = slog.Default()
	}
	cfg, err := Load(path)
	if err != nil {
		return nil, err
	}
	store := &Store{path: path, logger: logger}
	store.current.Store(cfg)
	return store, nil
}

// Current returns the current configuration.
func (s *Store) Current() *Config {
	return s.current.Load()
}

// Reload loads the configuration again. Invalid configurations are rejected and
// the previous configuration is kept.
func (s *Store) Reload() error {
	cfg, err := Load(s.path)
	if err != nil {
		return err
	}
	s.warnImmutable(s.Current(), cfg)
	s.current.Store(cfg)
	return nil
}

// Watch polls the configuration files until ctx is cancelled and reloads them
// when their contents change.
func (s *Store) Watch(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	last, err := s.digest()
	if err != nil {
		s.logger.Warn("failed to digest configuration files", "error", err)
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			digest, err := s.digest()
			if err != nil {
				s.logger.Warn("failed to digest configuration files", "error", err)
				continue
			}
			if digest == last {
				continue
			}
			if err := s.Reload(); err != nil {
				s.logger.Error("configuration reload failed; keeping previous configuration", "error", err)
				continue
			}
			last = digest
			s.logger.Info("configuration reloaded")
		}
	}
}

func (s *Store) digest() (string, error) {
	cfg := s.Current()
	files := []string{s.path}
	if cfg != nil && cfg.Policy.File != "" {
		files = append(files, cfg.Policy.File)
	}
	hash := sha256.New()
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			return "", err
		}
		_, _ = hash.Write(data)
		_, _ = hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// warnImmutable logs fields that cannot be changed without a restart.
func (s *Store) warnImmutable(old, updated *Config) {
	if old == nil {
		return
	}
	if old.Server != updated.Server {
		s.logger.Warn("server settings changed; restart required to apply them")
	}
	if old.GitHub.AppID != updated.GitHub.AppID ||
		old.GitHub.PrivateKeyPath != updated.GitHub.PrivateKeyPath ||
		old.GitHub.APIURL != updated.GitHub.APIURL ||
		old.GitHub.RequestTimeout != updated.GitHub.RequestTimeout {
		s.logger.Warn("github app settings changed; restart required to apply them")
	}
	if old.Auth != updated.Auth {
		s.logger.Warn("authentication settings changed; restart required to apply them")
	}
}
