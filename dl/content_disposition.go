package dl

import (
	"fmt"
	"net/url"
	"strings"
)

// ContentDisposition returns a Content-Disposition header value with both
// filename (ASCII fallback) and filename* (UTF-8 encoded) per RFC 6266.
// This ensures compatibility with wget, curl, and modern browsers.
func ContentDisposition(filename string) string {
	escaped := url.QueryEscape(filename)
	// For ASCII-only filenames, filename and filename* can be the same.
	// For non-ASCII filenames, filename uses ASCII approximation.
	asciiFilename := toASCIIFilename(filename)
	return fmt.Sprintf(`attachment; filename="%s"; filename*=UTF-8''%s`, asciiFilename, escaped)
}

// toASCIIFilename converts a filename to ASCII-safe version.
// For pure ASCII filenames, returns as-is.
// For non-ASCII, replaces non-ASCII chars with underscores.
func toASCIIFilename(filename string) string {
	var buf strings.Builder
	for _, r := range filename {
		if r > 127 {
			buf.WriteByte('_')
		} else {
			buf.WriteRune(r)
		}
	}
	return buf.String()
}
