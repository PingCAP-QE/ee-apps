package delivery

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// TiUP mirror names and the worker targets they map to.
//
// `staging` is http://tiup.pingcap.net:8988 and `prod` is
// http://tiup.pingcap.net:8987 (see the publisher design). A worker declares
// the target it has mirror credentials for, so the step target carries the
// prefix.
const (
	TiUPMirrorStaging = "staging"
	TiUPMirrorProd    = "prod"

	TiUPTargetStaging = "tiup-staging"
	TiUPTargetProd    = "tiup-prod"
)

// TiUPRule is one `tiup_publish_rules` entry.
type TiUPRule struct {
	Description string `yaml:"description,omitempty"`
	// TagsRegex are RE2 patterns matched against the artifact tag. The legacy
	// interpreter used Go's regexp, so these are not ECMAScript patterns.
	TagsRegex []string `yaml:"tags_regex"`
	// DestMirrors are the mirrors that receive the packages.
	DestMirrors []string `yaml:"dest_mirrors"`
	// Nightly marks the rule as a nightly build rule.
	Nightly bool `yaml:"nightly,omitempty"`
	// VersionRegexReplace overrides the published version. It is applied to
	// the artifact tag, so `$1` extracts a capture group.
	VersionRegexReplace *string `yaml:"version_regex_replace,omitempty"`
}

// TiUPPackage is one TiUP package extracted from an OCI artifact config.
type TiUPPackage struct {
	// Name is derived from the tarball file name, not from the config's `name`.
	Name        string `json:"name" yaml:"name"`
	Version     string `json:"version" yaml:"version"`
	OS          string `json:"os" yaml:"os"`
	Arch        string `json:"arch" yaml:"arch"`
	Description string `json:"description,omitempty" yaml:"description,omitempty"`
	EntryPoint  string `json:"entry_point" yaml:"entry_point"`
	File        string `json:"file" yaml:"file"`
	// Standalone is always false: the pre-migration publisher never forwarded
	// the artifact's standalone flag, and this port preserves that behaviour.
	Standalone bool `json:"standalone,omitempty" yaml:"standalone,omitempty"`
}

// TiUPPlan is the work for one destination mirror: every package of the
// artifact, at the version the rules resolved.
type TiUPPlan struct {
	// Target is the worker shard: tiup-staging or tiup-prod.
	Target string `json:"target" yaml:"target"`
	// Mirror is the bare mirror name from the rules: staging or prod.
	Mirror   string        `json:"mirror" yaml:"mirror"`
	Version  string        `json:"version" yaml:"version"`
	Nightly  bool          `json:"nightly" yaml:"nightly"`
	Packages []TiUPPackage `json:"packages" yaml:"packages"`
}

var (
	// tiupNameRegex extracts the package name from `name-vX.Y.Z-...tar.gz`.
	tiupNameRegex = regexp.MustCompile(`^(.+)-v\d+\.\d+\.\d+`)
	// tiupNightlyVersionRegex matches the versions the nightly rule publishes.
	tiupNightlyVersionRegex = regexp.MustCompile(`^v\d+\.\d+\.\d+.*(-nightly)$`)
	// tiupNightlyOSRegex strips the commit suffix of a nightly version.
	tiupNightlyOSRegex = regexp.MustCompile(`(-\d+-g[0-9a-f]{7,})$`)

	tiupGATagRegex      = regexp.MustCompile(`^(v\d+\.\d+\.\d+)(-\w+)?_(linux|darwin)_(amd64|arm64)$`)
	tiupBetaRCTagRegex  = regexp.MustCompile(`^(v\d+\.\d+\.\d+-(beta|rc)\.\d+)?(-\w+)?_(linux|darwin)_(amd64|arm64)$`)
	tiupNightlyTagRegex = regexp.MustCompile(`^(master|main)_(linux|darwin)_(amd64|arm64)$`)
)

// ErrNoTiUPArtifact is returned when an artifact config carries no TiUP
// package list; the caller treats it as "nothing to publish".
var ErrNoTiUPArtifact = errors.New("artifact has no net.pingcap.tibuild.tiup packages")

// MatchTiUP returns the destination mirrors and the version override for an
// artifact reference, mirroring the legacy interpreter:
//
//   - the repository is matched (unanchored) against every `tiup_publish_rules`
//     key;
//   - inside a matching repository, a rule matches when any of its tags_regex
//     matches the tag;
//   - the first matching pattern of a rule also derives the version override.
//
// Repository keys are visited in sorted order so the result does not depend on
// map iteration.
func (c *Config) MatchTiUP(artifactURL string) ([]TiUPTarget, error) {
	repo, tag, err := splitTiUPArtifact(artifactURL)
	if err != nil {
		return nil, err
	}

	keys := make([]string, 0, len(c.TiUPPublishRules))
	for key := range c.TiUPPublishRules {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	var targets []TiUPTarget
	seen := make(map[string]struct{})
	for _, key := range keys {
		repoRegex, err := regexp.Compile(key)
		if err != nil {
			return nil, fmt.Errorf("delivery: invalid tiup repository pattern %q: %w", key, err)
		}
		if !repoRegex.MatchString(repo) {
			continue
		}

		for ruleIndex, rule := range c.TiUPPublishRules[key] {
			matched, replaced := matchTiUPRule(rule, tag)
			if !matched {
				continue
			}
			for _, mirror := range rule.DestMirrors {
				target, err := tiupTarget(mirror)
				if err != nil {
					return nil, err
				}
				// Two patterns of the same rule can both match a tag; the
				// legacy interpreter queued the rule twice. The publish
				// intent is idempotent, so one entry per (rule, mirror) is
				// equivalent and keeps the plan deterministic.
				identity := fmt.Sprintf("%s|%d|%s", key, ruleIndex, target)
				if _, ok := seen[identity]; ok {
					continue
				}
				seen[identity] = struct{}{}
				targets = append(targets, TiUPTarget{
					Target:  target,
					Mirror:  mirror,
					Version: replaced,
					Nightly: rule.Nightly,
				})
			}
		}
	}

	return targets, nil
}

// TiUPTarget is one destination mirror resolved for an artifact.
type TiUPTarget struct {
	Target string
	Mirror string
	// Version is the rule's override; empty means "use the artifact version".
	Version string
	Nightly bool
}

// PlanTiUP turns an artifact reference and its OCI config into the complete
// publish plan, one entry per destination mirror.
func (c *Config) PlanTiUP(artifactURL string, artifactConfig map[string]any) ([]TiUPPlan, error) {
	targets, err := c.MatchTiUP(artifactURL)
	if err != nil {
		return nil, err
	}
	if len(targets) == 0 {
		return nil, nil
	}

	_, tag, err := splitTiUPArtifact(artifactURL)
	if err != nil {
		return nil, err
	}

	packages, err := TiUPPackages(artifactConfig, tag)
	if err != nil {
		return nil, err
	}

	plans := make([]TiUPPlan, 0, len(targets))
	for _, target := range targets {
		version := target.Version
		if version == "" {
			version = packagesVersion(artifactConfig, tag)
		}
		resolved := make([]TiUPPackage, 0, len(packages))
		for _, pkg := range packages {
			pkg.Version = version
			resolved = append(resolved, pkg)
		}
		plans = append(plans, TiUPPlan{
			Target:   target.Target,
			Mirror:   target.Mirror,
			Version:  version,
			Nightly:  target.Nightly,
			Packages: resolved,
		})
	}

	return plans, nil
}

// TiUPPackages extracts the TiUP package list from an artifact config.
func TiUPPackages(config map[string]any, tag string) ([]TiUPPackage, error) {
	raw, ok := config["net.pingcap.tibuild.tiup"].([]any)
	if !ok || len(raw) == 0 {
		return nil, ErrNoTiUPArtifact
	}

	osName, ok := config["net.pingcap.tibuild.os"].(string)
	if !ok || osName == "" {
		return nil, errors.New("artifact config is missing net.pingcap.tibuild.os")
	}
	arch, ok := config["net.pingcap.tibuild.architecture"].(string)
	if !ok || arch == "" {
		return nil, errors.New("artifact config is missing net.pingcap.tibuild.architecture")
	}
	version := packagesVersion(config, tag)

	packages := make([]TiUPPackage, 0, len(raw))
	for i, entry := range raw {
		pkg, ok := entry.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("artifact config tiup entry %d is not an object", i)
		}
		file, _ := pkg["file"].(string)
		if file == "" {
			return nil, fmt.Errorf("artifact config tiup entry %d has no file", i)
		}
		entryPoint, _ := pkg["entrypoint"].(string)
		description, _ := pkg["description"].(string)
		packages = append(packages, TiUPPackage{
			Name:        tiupPkgName(file),
			Version:     version,
			OS:          osName,
			Arch:        arch,
			Description: description,
			EntryPoint:  entryPoint,
			File:        file,
		})
	}

	for _, pkg := range packages {
		if pkg.Name == "" {
			return nil, fmt.Errorf("cannot derive a package name from %q", pkg.File)
		}
	}
	return packages, nil
}

// packagesVersion derives the published version from the artifact config,
// applying the nightly transformation for nightly tags.
func packagesVersion(config map[string]any, tag string) string {
	version, _ := config["org.opencontainers.image.version"].(string)
	return transformTiUPVersion(version, tag)
}

// transformTiUPVersion reproduces the legacy version derivation.
func transformTiUPVersion(version, tag string) string {
	switch {
	case tiupGATagRegex.MatchString(tag):
		matches := tiupGATagRegex.FindStringSubmatch(tag)
		return strings.Join(matches[1:3], "")
	case tiupBetaRCTagRegex.MatchString(tag):
		matches := tiupBetaRCTagRegex.FindStringSubmatch(tag)
		return strings.Join([]string{matches[1], matches[3]}, "")
	case tiupNightlyTagRegex.MatchString(tag):
		return tiupNightlyOSRegex.ReplaceAllString(version, "") + "-nightly"
	default:
		return version
	}
}

// matchTiUPRule reports whether the rule matches the tag and returns the
// version override derived from the last matching pattern.
func matchTiUPRule(rule TiUPRule, tag string) (bool, string) {
	matched := false
	replaced := ""
	for _, pattern := range rule.TagsRegex {
		tagRegex, err := regexp.Compile(pattern)
		if err != nil {
			// An invalid pattern must not silently broaden the rule.
			continue
		}
		if !tagRegex.MatchString(tag) {
			continue
		}
		matched = true
		if rule.VersionRegexReplace != nil {
			replaced = tagRegex.ReplaceAllString(tag, *rule.VersionRegexReplace)
		}
	}
	return matched, replaced
}

// IsNightlyTiUP reports whether a resolved version is a nightly one.
func IsNightlyTiUP(version string) bool {
	return tiupNightlyVersionRegex.MatchString(version)
}

// tiupPkgName derives the package name from the tarball file name.
func tiupPkgName(file string) string {
	matches := tiupNameRegex.FindStringSubmatch(baseName(file))
	if len(matches) > 1 {
		return matches[1]
	}
	return ""
}

func baseName(path string) string {
	if idx := strings.LastIndex(path, "/"); idx >= 0 {
		return path[idx+1:]
	}
	return path
}

// tiupTarget validates a mirror name and maps it to the worker target.
func tiupTarget(mirror string) (string, error) {
	switch mirror {
	case TiUPMirrorStaging:
		return TiUPTargetStaging, nil
	case TiUPMirrorProd:
		return TiUPTargetProd, nil
	default:
		return "", fmt.Errorf("delivery: unknown tiup mirror %q", mirror)
	}
}

// splitTiUPArtifact splits an OCI reference into repository and tag.
func splitTiUPArtifact(artifactURL string) (string, string, error) {
	ref, err := ParseImageRef(artifactURL)
	if err != nil {
		return "", "", err
	}
	return ref.Repository, ref.Tag, nil
}
