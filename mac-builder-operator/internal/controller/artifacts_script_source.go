package controller

import (
	"fmt"
	"regexp"
	"strings"
)

const (
	DefaultArtifactsScriptRepoURL = "https://github.com/PingCAP-QE/artifacts.git"
	// DefaultArtifactsScriptRepoRevision tracks the artifacts repo main branch. The
	// repo is continuously updated, so the build scripts are intentionally not pinned
	// by default; set ExpectedCommit (or pass a full SHA/tag) to pin for reproducibility.
	DefaultArtifactsScriptRepoRevision   = "main"
	DefaultArtifactsScriptExpectedCommit = ""
)

var fullCommitSHARegexp = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)

// ArtifactsScriptSourceConfig pins the external artifacts repo used to generate build scripts.
type ArtifactsScriptSourceConfig struct {
	URL            string
	Revision       string
	ExpectedCommit string
}

// Normalize applies defaults. A branch (e.g. main), a tag, or a full commit SHA
// is accepted; when ExpectedCommit is set the checked-out HEAD must match it.
func (c ArtifactsScriptSourceConfig) Normalize() (ArtifactsScriptSourceConfig, error) {
	normalized := ArtifactsScriptSourceConfig{
		URL:            strings.TrimSpace(c.URL),
		Revision:       strings.TrimSpace(c.Revision),
		ExpectedCommit: strings.TrimSpace(c.ExpectedCommit),
	}

	if normalized.URL == "" {
		normalized.URL = DefaultArtifactsScriptRepoURL
	}
	if normalized.Revision == "" {
		if normalized.ExpectedCommit != "" {
			normalized.Revision = normalized.ExpectedCommit
		} else {
			normalized.Revision = DefaultArtifactsScriptRepoRevision
		}
	}

	if normalized.ExpectedCommit != "" && !isFullCommitSHA(normalized.ExpectedCommit) {
		return ArtifactsScriptSourceConfig{}, fmt.Errorf(
			"artifacts repo expected commit %q must be a full 40-character SHA",
			normalized.ExpectedCommit,
		)
	}
	// Pin to the revision when it is a full commit SHA and no expected commit was given.
	if normalized.ExpectedCommit == "" && isFullCommitSHA(normalized.Revision) {
		normalized.ExpectedCommit = strings.ToLower(normalized.Revision)
	}

	return normalized, nil
}

func isFullCommitSHA(value string) bool {
	return fullCommitSHARegexp.MatchString(strings.TrimSpace(value))
}
