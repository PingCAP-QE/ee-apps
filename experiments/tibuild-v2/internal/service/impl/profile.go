package impl

import "regexp"

const (
	nextgenEdition       = "nextgen"
	legacyNextgenEdition = "next-gen"
)

var calendarTidbxImageTagRegexp = regexp.MustCompile(`^(v[2-9][0-9]\.(?:1[0-2]|[1-9])\.\d+)-nextgen$`)

func normalizeEdition(edition string) string {
	switch edition {
	case nextgenEdition, legacyNextgenEdition:
		return nextgenEdition
	default:
		return edition
	}
}

func normalizeTidbxQueryTag(tag string) string {
	matches := calendarTidbxImageTagRegexp.FindStringSubmatch(tag)
	if len(matches) == 2 {
		return matches[1]
	}
	return tag
}
