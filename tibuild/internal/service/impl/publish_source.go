package impl

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/distribution/reference"
)

// validateTiUPHTTPPublishSource enforces the source policy for an `http` TiUP
// publish source (`from.http.url`).
//
// The publish worker downloads that URL and hands the bytes to
// `tiup mirror publish`, and the mirror signing key is mounted in the worker.
// An unvalidated URL is therefore two primitives at once:
//
//   - SSRF — the worker will fetch any scheme/host it is given, including
//     cluster-internal services and cloud metadata endpoints;
//   - supply-chain write — whoever can reach the unauthenticated publish route
//     can make the mirror serve bytes they chose, for a package name of their
//     choosing.
//
// The host allowlist therefore fails closed: an empty list rejects every `http`
// source instead of accepting everything. The `oci` source does not need this
// check because it already pins a tag or digest.
//
// Allowlist entries are exact hostnames; a leading `*.` also allows subdomains.
// Matching is case-insensitive and ignores the port. Credentials in the URL
// (`https://user:pass@host/`) are rejected: they would be logged and are not
// part of the publish contract.
func validateTiUPHTTPPublishSource(allowedHosts []string, rawURL string) error {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" {
		return fmt.Errorf("from.http.url is required for a http source")
	}

	parsed, err := url.Parse(trimmed)
	if err != nil {
		return fmt.Errorf("from.http.url is not a valid URL: %v", err)
	}
	switch parsed.Scheme {
	case "http", "https":
	default:
		return fmt.Errorf("from.http.url scheme %q is not allowed, use http or https", parsed.Scheme)
	}
	if parsed.User != nil {
		return fmt.Errorf("from.http.url must not carry credentials")
	}
	host := strings.ToLower(parsed.Hostname())
	if host == "" {
		return fmt.Errorf("from.http.url has no host")
	}

	normalized := make([]string, 0, len(allowedHosts))
	for _, entry := range allowedHosts {
		if candidate, ok := normalizePublishSourceHost(entry); ok {
			normalized = append(normalized, candidate)
		}
	}
	if len(normalized) == 0 {
		return fmt.Errorf(
			"http publish sources are disabled: publish.http_source_allowed_hosts is empty (source host %q rejected)",
			host)
	}

	for _, entry := range normalized {
		if entry == host {
			return nil
		}
		if suffix, wildcard := strings.CutPrefix(entry, "*."); wildcard {
			// "*.example.com" covers "a.example.com" and "a.b.example.com"
			// but never "example.com" itself nor "evilexample.com".
			if suffix != "" && strings.HasSuffix(host, "."+suffix) {
				return nil
			}
		}
	}

	return fmt.Errorf("from.http.url host %q is not in publish.http_source_allowed_hosts (%s)",
		host, strings.Join(normalized, ", "))
}

// normalizePublishSourceHost makes one configured allowlist entry comparable
// with `url.Hostname()`: trimmed, lower case, no trailing dot or slash. A
// leading `*.` wildcard marker is preserved.
func normalizePublishSourceHost(entry string) (string, bool) {
	trimmed := strings.ToLower(strings.TrimSpace(entry))
	trimmed = strings.TrimSuffix(trimmed, "/")
	wildcard := strings.HasPrefix(trimmed, "*.")
	trimmed = strings.TrimPrefix(trimmed, "*.")
	trimmed = strings.TrimSuffix(strings.TrimSuffix(trimmed, "."), "/")
	if trimmed == "" {
		return "", false
	}
	// A configured entry may carry a port; the comparison is host-only.
	if host, _, found := strings.Cut(trimmed, ":"); found {
		trimmed = host
	}
	if trimmed == "" {
		return "", false
	}
	if wildcard {
		return "*." + trimmed, true
	}
	return trimmed, true
}

// validateTiUPOCIPublishSource enforces the source policy for an OCI TiUP
// publish source (`artifact_url` of POST /publish/tiup/publish-request).
//
// Pinning a tag or digest fixes what gets published, not where it is read from:
// the worker pulls the artifact with its own registry credentials, so a caller
// naming an arbitrary host turns the worker into a fetch primitive against any
// registry it can reach. The host allowlist therefore fails closed.
func validateTiUPOCIPublishSource(allowedHosts []string, artifactURL string) error {
	trimmed := strings.TrimSpace(artifactURL)
	if trimmed == "" {
		return fmt.Errorf("artifact_url is required for an oci source")
	}
	if strings.Contains(trimmed, "://") {
		return fmt.Errorf("artifact_url must be an OCI reference, not a URL")
	}

	named, err := reference.ParseNormalizedNamed(trimmed)
	if err != nil {
		return fmt.Errorf("artifact_url is not a valid OCI reference: %v", err)
	}
	host := strings.ToLower(reference.Domain(named))

	normalized := make([]string, 0, len(allowedHosts))
	for _, entry := range allowedHosts {
		if candidate, ok := normalizePublishSourceHost(entry); ok {
			normalized = append(normalized, candidate)
		}
	}
	if len(normalized) == 0 {
		return fmt.Errorf(
			"oci publish sources are disabled: publish.oci_source_allowed_hosts is empty (registry %q rejected)",
			host)
	}

	for _, entry := range normalized {
		if entry == host {
			return nil
		}
		if suffix, wildcard := strings.CutPrefix(entry, "*."); wildcard {
			if suffix != "" && strings.HasSuffix(host, "."+suffix) {
				return nil
			}
		}
	}
	return fmt.Errorf("artifact_url registry %q is not in publish.oci_source_allowed_hosts (%s)",
		host, strings.Join(normalized, ", "))
}
