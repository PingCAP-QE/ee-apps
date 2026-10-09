package controller

import (
	"fmt"
	"regexp"
	"strings"
)

const (
	DefaultArtifactsScriptRepoURL        = "https://github.com/PingCAP-QE/artifacts.git"
	DefaultArtifactsScriptRepoRevision   = "4f25d0e06aec38fbefb5c02513964571eb13eea7"
	DefaultArtifactsScriptExpectedCommit = DefaultArtifactsScriptRepoRevision
)

var fullCommitSHARegexp = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)

// ArtifactsScriptSourceConfig pins the external artifacts repo used to generate build scripts.
type ArtifactsScriptSourceConfig struct {
	URL            string
	Revision       string
	ExpectedCommit string
}

// Normalize applies defaults and rejects mutable refs before a worker executes external scripts.
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

	if normalized.ExpectedCommit == "" {
		switch {
		case normalized.Revision == DefaultArtifactsScriptRepoRevision:
			normalized.ExpectedCommit = DefaultArtifactsScriptExpectedCommit
		case isFullCommitSHA(normalized.Revision):
			normalized.ExpectedCommit = normalized.Revision
		default:
			return ArtifactsScriptSourceConfig{}, fmt.Errorf(
				"artifacts repo expected commit must be set when revision %q is not a full commit SHA",
				normalized.Revision,
			)
		}
	}

	if isMutableGitRevision(normalized.Revision) {
		return ArtifactsScriptSourceConfig{}, fmt.Errorf(
			"artifacts repo revision %q must be an immutable commit or tag; branch refs are not allowed",
			normalized.Revision,
		)
	}
	if !isFullCommitSHA(normalized.ExpectedCommit) {
		return ArtifactsScriptSourceConfig{}, fmt.Errorf(
			"artifacts repo expected commit %q must be a full 40-character SHA",
			normalized.ExpectedCommit,
		)
	}

	normalized.ExpectedCommit = strings.ToLower(normalized.ExpectedCommit)
	return normalized, nil
}

func isFullCommitSHA(value string) bool {
	return fullCommitSHARegexp.MatchString(strings.TrimSpace(value))
}

func isMutableGitRevision(value string) bool {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" || isFullCommitSHA(trimmed) {
		return false
	}

	lower := strings.ToLower(trimmed)
	return lower == "main" ||
		lower == "master" ||
		strings.HasPrefix(lower, "refs/heads/") ||
		strings.HasPrefix(lower, "refs/remotes/") ||
		strings.HasPrefix(lower, "origin/")
}
