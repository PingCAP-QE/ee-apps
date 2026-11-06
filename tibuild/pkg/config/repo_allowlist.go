package config

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// repoEntryPattern is the accepted form of one allowlist entry: "<owner>/<repo>"
// with an optional single "*" wildcard for the repository segment. The owner
// and repo segments are the characters GitHub allows in account and repository
// names; a partial glob such as "pingcap/tid*" is rejected rather than silently
// treated as a literal.
var repoEntryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+|\*)$`)

// RepoAllowlist is an immutable set of GitHub repositories a devbuild request
// may name. It is built once from configuration and then only read, so it can
// be published through an atomic pointer while requests are in flight.
//
// The zero value is a valid empty list that allows nothing.
type RepoAllowlist struct {
	// exact holds lower-cased "<owner>/<repo>" entries.
	exact map[string]struct{}
	// owners holds lower-cased owners whose every repository is allowed.
	owners map[string]struct{}
}

// NewRepoAllowlist validates and normalises the configured entries. Validation
// is strict on purpose: a typo must fail the configuration revision instead of
// silently widening or narrowing what callers may build.
func NewRepoAllowlist(entries []string) (RepoAllowlist, error) {
	allow := RepoAllowlist{}
	for _, entry := range entries {
		trimmed := strings.TrimSpace(entry)
		if !repoEntryPattern.MatchString(trimmed) {
			return RepoAllowlist{}, fmt.Errorf(
				"invalid repository %q: want <owner>/<repo> or <owner>/*", entry)
		}
		owner, repo, _ := strings.Cut(trimmed, "/")
		owner = strings.ToLower(owner)
		if repo == "*" {
			if allow.owners == nil {
				allow.owners = map[string]struct{}{}
			}
			allow.owners[owner] = struct{}{}
			continue
		}
		if allow.exact == nil {
			allow.exact = map[string]struct{}{}
		}
		allow.exact[owner+"/"+strings.ToLower(repo)] = struct{}{}
	}
	return allow, nil
}

// Empty reports whether the list contains no entries. An empty list means
// "only the product default repository", see DevBuild.Validate.
func (a RepoAllowlist) Empty() bool {
	return len(a.exact) == 0 && len(a.owners) == 0
}

// Allows reports whether repo matches an entry. Matching is case-insensitive
// and ignores surrounding spaces. "<owner>/*" covers any single repository
// segment of that owner, never a nested path.
func (a RepoAllowlist) Allows(repo string) bool {
	trimmed := strings.TrimSpace(repo)
	owner, name, ok := strings.Cut(trimmed, "/")
	if !ok || owner == "" || name == "" || strings.Contains(name, "/") {
		return false
	}
	owner = strings.ToLower(owner)
	if _, ok := a.owners[owner]; ok {
		return true
	}
	_, ok = a.exact[owner+"/"+strings.ToLower(name)]
	return ok
}

// Validate checks the devbuild repository allowlist and its coupling with the
// product default repositories.
//
// A non-empty list must cover every value of product_repo_map: the product
// default bypasses the allowlist lookup, so an uncovered default would be the
// one repository a caller could still reach implicitly. An empty list skips
// that check, because implicit use of the defaults is then the only thing the
// request path accepts.
func (d DevBuild) Validate(productRepoMap map[string]string) error {
	if len(d.GithubRepoAllowlist) == 0 {
		return nil
	}
	allow, err := NewRepoAllowlist(d.GithubRepoAllowlist)
	if err != nil {
		return fmt.Errorf("devbuild.github_repo_allowlist: %w", err)
	}

	products := make([]string, 0, len(productRepoMap))
	for product := range productRepoMap {
		products = append(products, product)
	}
	sort.Strings(products)
	for _, product := range products {
		repo := productRepoMap[product]
		if !allow.Allows(repo) {
			return fmt.Errorf(
				"devbuild.github_repo_allowlist does not cover product_repo_map[%s]=%q: add that repository to the allowlist",
				product, repo)
		}
	}
	return nil
}
