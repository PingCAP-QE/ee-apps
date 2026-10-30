// Package delivery interprets the delivery rules maintained in
// PingCAP-QE/artifacts (packages/delivery.yaml).
//
// The rules decide how a built artifact is delivered: which destination
// repositories/mirrors receive it and under which tags. Historically three
// interpreters existed (a deno script for images, a shell script for TiUP and a
// Go copy inside publisher). This package is the single implementation both the
// image planner and the TiUP publisher use.
package delivery

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config is the decoded delivery.yaml.
type Config struct {
	// Deprecated lists source artefacts that must not be delivered anymore.
	// They are kept in the rules file so the build keeps working while the
	// pipeline catches up.
	Deprecated []string `yaml:"deprecated"`

	// ImageCopyRules maps a source repository (without tag) to the rules that
	// decide where its tags are copied.
	ImageCopyRules map[string][]ImageRule `yaml:"image_copy_rules"`
}

// ImageRule is one `image_copy_rules` entry. YAML anchors and merge keys are
// already resolved by the decoder, so a rule always carries its full set of
// fields (see ImageDefinitions in the rules file).
type ImageRule struct {
	Description string `yaml:"description,omitempty"`
	// TagsRegex are ECMAScript regular expressions; a rule matches when at
	// least one of them matches the tag.
	TagsRegex        []string `yaml:"tags_regex"`
	DestRepositories []string `yaml:"dest_repositories"`
	// ConstantTags are additional tags written on every destination, e.g.
	// `nightly` for trunk builds.
	ConstantTags []string `yaml:"constant_tags,omitempty"`
	// TagRegexReplace derives one more tag from the first matching regex, e.g.
	// `$1` turns `v8.5.0-enterprise` into `v8.5.0`.
	TagRegexReplace string `yaml:"tag_regex_replace,omitempty"`
}

// LoadFile decodes the rules file at path.
func LoadFile(path string) (*Config, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("delivery: read rules: %w", err)
	}
	cfg, err := Parse(content)
	if err != nil {
		return nil, fmt.Errorf("delivery: parse %s: %w", path, err)
	}
	return cfg, nil
}

// Parse decodes delivery rules from YAML.
func Parse(content []byte) (*Config, error) {
	var cfg Config
	if err := yaml.Unmarshal(content, &cfg); err != nil {
		return nil, err
	}
	if len(cfg.ImageCopyRules) == 0 && len(cfg.Deprecated) == 0 {
		return nil, fmt.Errorf("no delivery rules found")
	}
	return &cfg, nil
}

// IsDeprecated reports whether repo must not be delivered. Entries are matched
// on path boundaries, so a deprecated `.../widget/image` also covers
// `.../widget/image/sub` but not `.../widget/image2`.
func (c *Config) IsDeprecated(repo string) bool {
	if c == nil {
		return false
	}
	repo = strings.TrimSuffix(repo, "/")
	for _, entry := range c.Deprecated {
		entry = strings.TrimSuffix(entry, "/")
		if entry == "" {
			continue
		}
		if repo == entry || strings.HasPrefix(repo, entry+"/") {
			return true
		}
	}
	return false
}

// Targets returns the sorted, de-duplicated registry hosts used as destinations
// by the image copy rules.
//
// Workers declare the targets they serve (credentials + network reachability),
// so this list is the catalogue their configuration is validated against.
func (c *Config) Targets() []string {
	if c == nil {
		return nil
	}
	seen := map[string]struct{}{}
	for _, rules := range c.ImageCopyRules {
		for _, rule := range rules {
			for _, dest := range rule.DestRepositories {
				seen[TargetOf(dest)] = struct{}{}
			}
		}
	}
	targets := make([]string, 0, len(seen))
	for target := range seen {
		targets = append(targets, target)
	}
	sort.Strings(targets)
	return targets
}
