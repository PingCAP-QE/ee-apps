package impl

import (
	"strings"

	"golang.org/x/mod/semver"
)

const (
	nextgenEdition            = "nextgen"
	legacyNextgenEdition      = "next-gen"
	tidbxCalendarTagThreshold = "v26.0.0"
)

func normalizeEdition(edition string) string {
	switch edition {
	case nextgenEdition, legacyNextgenEdition:
		return nextgenEdition
	default:
		return edition
	}
}

func isPlainSemverTag(tag string) bool {
	return semver.IsValid(tag) && !strings.Contains(strings.TrimPrefix(tag, "v"), "-")
}

func isNewTidbxGitTag(tag string) bool {
	return isPlainSemverTag(tag) && semver.Compare(tag, tidbxCalendarTagThreshold) >= 0
}

func normalizeTidbxQueryTag(tag string) string {
	if !strings.HasSuffix(tag, "-nextgen") {
		return tag
	}

	baseTag := strings.TrimSuffix(tag, "-nextgen")
	if isNewTidbxGitTag(baseTag) {
		return baseTag
	}

	return tag
}
