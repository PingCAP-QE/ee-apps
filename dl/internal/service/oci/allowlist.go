package oci

import (
	"errors"
	"net"
	"strings"
)

var (
	// ErrTargetNotAllowed is returned when a repository points at a registry
	// that is not explicitly allowlisted.
	ErrTargetNotAllowed = errors.New("target registry is not allowed")
	// ErrInvalidRepository is returned when a repository string is malformed.
	ErrInvalidRepository = errors.New("invalid OCI repository")
)

// ValidateTargetRepository checks that repo is a well-formed OCI repository
// whose registry host is present in allowed. It never performs DNS resolution.
func ValidateTargetRepository(repo string, allowed []string) error {
	host, err := parseRepositoryHost(repo)
	if err != nil {
		return err
	}
	if !hostAllowed(host, allowed) {
		return ErrTargetNotAllowed
	}
	return nil
}

func parseRepositoryHost(repo string) (string, error) {
	repo = strings.TrimSpace(repo)
	if repo == "" {
		return "", ErrInvalidRepository
	}
	if strings.Contains(repo, "://") ||
		strings.Contains(repo, "@") ||
		strings.ContainsAny(repo, "?#") {
		return "", ErrInvalidRepository
	}

	parts := strings.SplitN(repo, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", ErrInvalidRepository
	}
	return normalizeRegistryHost(parts[0])
}

func normalizeRegistryHost(host string) (string, error) {
	host = strings.TrimSuffix(strings.ToLower(strings.TrimSpace(host)), ".")
	if host == "" || strings.HasPrefix(host, "[") {
		return "", ErrTargetNotAllowed
	}

	if strings.Contains(host, ":") {
		var err error
		host, _, err = net.SplitHostPort(host)
		if err != nil {
			return "", ErrInvalidRepository
		}
	}

	if net.ParseIP(host) != nil {
		return "", ErrTargetNotAllowed
	}
	if host == "localhost" ||
		host == "metadata.google.internal" ||
		strings.HasSuffix(host, ".svc") ||
		strings.HasSuffix(host, ".local") {
		return "", ErrTargetNotAllowed
	}
	return host, nil
}

func hostAllowed(host string, allowed []string) bool {
	for _, candidate := range allowed {
		normalized, err := normalizeRegistryHost(candidate)
		if err != nil {
			continue
		}
		if normalized == host {
			return true
		}
	}
	return false
}

// IsTargetError reports whether err is a target allowlist or repository
// validation error. Callers use it to map these failures to 4xx responses.
func IsTargetError(err error) bool {
	return errors.Is(err, ErrTargetNotAllowed) || errors.Is(err, ErrInvalidRepository)
}
