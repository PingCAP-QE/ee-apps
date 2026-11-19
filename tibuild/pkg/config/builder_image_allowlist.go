package config

import (
	"fmt"
	"strings"

	"github.com/distribution/reference"
)

// BuilderImageAllowlist is an immutable set of repository prefixes a devbuild
// may name in `builderImg`. Only the repository is matched: the tag or digest
// of the requested image is free, and an entry that carries one is rejected.
//
// The zero value allows nothing. Callers treat an empty list as "the policy is
// off" (the legacy behaviour), see DevBuild.Validate.
type BuilderImageAllowlist struct {
	// prefixes holds normalised repository prefixes, e.g.
	// "ghcr.io/pingcap-qe".
	prefixes []string
}

// NewBuilderImageAllowlist validates and normalises the configured entries.
// Validation is strict on purpose: an entry that can never match (a tag, a
// glob, a bare name that would silently become a docker.io/library path) would
// otherwise leave the policy looking configured while matching nothing.
func NewBuilderImageAllowlist(entries []string) (BuilderImageAllowlist, error) {
	allow := BuilderImageAllowlist{}
	for _, entry := range entries {
		// Image reference names are lower case; accept a human-friendly entry
		// and compare the normalised form, as the matcher does.
		trimmed := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(entry), "/"))
		if trimmed == "" {
			return BuilderImageAllowlist{}, fmt.Errorf("empty repository entry")
		}
		if strings.Contains(trimmed, "*") {
			return BuilderImageAllowlist{}, fmt.Errorf(
				"invalid repository %q: wildcards are not supported, list the namespace instead (for example %q)",
				entry, strings.TrimSuffix(trimmed, "/*"))
		}
		if !strings.Contains(trimmed, "/") {
			return BuilderImageAllowlist{}, fmt.Errorf(
				"invalid repository %q: list a namespace under its registry (for example \"ghcr.io/pingcap-qe\", or \"ghcr.io/pingcap-qe/*\" for the whole account)",
				entry)
		}
		if !hasRegistry(trimmed) {
			return BuilderImageAllowlist{}, fmt.Errorf(
				"invalid repository %q: qualify it with its registry (for example \"docker.io/%s\")",
				entry, trimmed)
		}
		named, err := reference.ParseNormalizedNamed(trimmed)
		if err != nil {
			return BuilderImageAllowlist{}, fmt.Errorf("invalid repository %q: %w", entry, err)
		}
		if _, tagged := named.(reference.Tagged); tagged {
			return BuilderImageAllowlist{}, fmt.Errorf(
				"invalid repository %q: remove the tag; only the repository is matched", entry)
		}
		if _, digested := named.(reference.Digested); digested {
			return BuilderImageAllowlist{}, fmt.Errorf(
				"invalid repository %q: remove the digest; only the repository is matched", entry)
		}
		allow.prefixes = append(allow.prefixes, reference.TrimNamed(named).Name())
	}
	return allow, nil
}

// Empty reports whether the list contains no entries.
func (a BuilderImageAllowlist) Empty() bool {
	return len(a.prefixes) == 0
}

// Allows reports whether repository is one of the configured namespaces or
// below one of them. The caller passes the repository of an already parsed
// image reference, without a tag or digest.
func (a BuilderImageAllowlist) Allows(repository string) bool {
	normalized := strings.ToLower(strings.TrimSpace(repository))
	for _, prefix := range a.prefixes {
		// Compare whole path segments so "ghcr.io/pingcap-qe" never matches
		// "ghcr.io/pingcap-qe-evil/...".
		if normalized == prefix || strings.HasPrefix(normalized, prefix+"/") {
			return true
		}
	}
	return false
}

// Entries returns the normalised prefixes. It is meant for log and error
// messages, not for matching.
func (a BuilderImageAllowlist) Entries() []string {
	return append([]string(nil), a.prefixes...)
}

// hasRegistry reports whether the first path segment names a registry that
// `reference.ParseNormalizedNamed` would keep, mirroring its own detection
// rule. Without it a bare "pingcap/x" would silently normalise to
// "docker.io/pingcap/x" while the operator meant an internal registry.
func hasRegistry(entry string) bool {
	first, _, _ := strings.Cut(entry, "/")
	return strings.ContainsAny(first, ".:") || first == "localhost"
}
