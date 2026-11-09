// Package share implements durable delivery grants. Plaintext capabilities are
// returned once and are never persisted or included in audit events.
package share

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"net/url"
	"os"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
	"oras.land/oras-go/v2/registry"
)

type Policy struct {
	ID            string   `yaml:"id"`
	TokenSHA256   string   `yaml:"token_sha256"`
	Repositories  []string `yaml:"repositories"`
	AllowedTags   string   `yaml:"allowed_tags"`
	MaxTTLSeconds int64    `yaml:"max_ttl_seconds"`
	hash          [32]byte
	tags          *regexp.Regexp
}

type Config struct {
	PublicURL string   `yaml:"public_url"`
	Issuers   []Policy `yaml:"issuers"`
}

func LoadConfig(filename string) (*Config, error) {
	f, err := os.Open(filename)
	if err != nil {
		return nil, errors.New("cannot read delivery policy")
	}
	defer f.Close()
	var cfg Config
	d := yaml.NewDecoder(f)
	d.KnownFields(true)
	if err := d.Decode(&cfg); err != nil {
		return nil, errors.New("invalid delivery policy")
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func (c *Config) Validate() error {
	u, err := url.Parse(c.PublicURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return errors.New("delivery public_url must be an HTTPS origin")
	}
	if origin := "https://" + u.Host; c.PublicURL != origin && c.PublicURL != origin+"/" {
		return errors.New("delivery public_url must be an HTTPS origin")
	}
	c.PublicURL = strings.TrimSuffix(c.PublicURL, "/")
	if len(c.Issuers) == 0 {
		return errors.New("delivery requires issuer policies")
	}
	ids := map[string]bool{}
	hashes := map[[32]byte]bool{}
	for i := range c.Issuers {
		p := &c.Issuers[i]
		b, err := hex.DecodeString(p.TokenSHA256)
		if err != nil || len(b) != 32 || p.ID == "" || len(p.ID) > 128 || ids[p.ID] || len(p.Repositories) == 0 || p.AllowedTags == "" || p.MaxTTLSeconds < 1 || p.MaxTTLSeconds > 30*86400 {
			return errors.New("invalid delivery issuer policy")
		}
		copy(p.hash[:], b)
		if hashes[p.hash] {
			return errors.New("duplicate delivery issuer credential")
		}
		hashes[p.hash], ids[p.ID] = true, true
		p.tags, err = regexp.Compile("^(?:" + p.AllowedTags + ")$")
		if err != nil {
			return errors.New("invalid delivery tag policy")
		}
		for _, repo := range p.Repositories {
			if !validRepository(strings.TrimSuffix(repo, "/")) {
				return errors.New("invalid delivery repository policy")
			}
		}
	}
	return nil
}

func (c *Config) Authenticate(secret string) *Policy {
	h := sha256.Sum256([]byte(secret))
	var found *Policy
	for i := range c.Issuers {
		if subtle.ConstantTimeCompare(h[:], c.Issuers[i].hash[:]) == 1 {
			found = &c.Issuers[i]
		}
	}
	return found
}

func (c *Config) Issuer(id string) *Policy {
	for i := range c.Issuers {
		if c.Issuers[i].ID == id {
			return &c.Issuers[i]
		}
	}
	return nil
}

func validRepository(repo string) bool {
	r, err := registry.ParseReference(repo)
	return err == nil && r.Reference == "" && repo == r.Registry+"/"+r.Repository
}

var tagPattern = regexp.MustCompile(`^[\w][\w.-]{0,127}$`)

func (p *Policy) Allows(repository, tag string) bool {
	if !validRepository(repository) || !tagPattern.MatchString(tag) || !p.tags.MatchString(tag) {
		return false
	}
	for _, scope := range p.Repositories {
		if repository == scope || (strings.HasSuffix(scope, "/") && strings.HasPrefix(repository, scope)) {
			return true
		}
	}
	return false
}
