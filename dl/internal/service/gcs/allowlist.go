package gcs

import (
	"errors"
	"strings"
)

var (
	// ErrTargetNotAllowed is returned when a bucket is not explicitly
	// allowlisted.
	ErrTargetNotAllowed = errors.New("target bucket is not allowed")
	// ErrInvalidObjectKey is returned when an object key is empty or does not
	// match the configured prefix allowlist.
	ErrInvalidObjectKey = errors.New("invalid object key")
	// ErrObjectNotFound hides upstream GCS details from callers.
	ErrObjectNotFound = errors.New("object not found")
)

// ValidateBucket checks bucket against an exact, case-insensitive allowlist.
func ValidateBucket(bucket string, allowed []string) error {
	bucket = strings.ToLower(strings.TrimSpace(bucket))
	if bucket == "" || strings.ContainsAny(bucket, "/\\*?") {
		return ErrTargetNotAllowed
	}
	for _, candidate := range allowed {
		if strings.EqualFold(strings.TrimSpace(candidate), bucket) {
			return nil
		}
	}
	return ErrTargetNotAllowed
}

// ValidateObjectKey enforces an optional key prefix allowlist.
func ValidateObjectKey(key string, prefixes []string) error {
	key = strings.TrimSpace(key)
	if key == "" {
		return ErrInvalidObjectKey
	}
	if len(prefixes) == 0 {
		return nil
	}
	for _, prefix := range prefixes {
		if prefix != "" && strings.HasPrefix(key, prefix) {
			return nil
		}
	}
	return ErrInvalidObjectKey
}

func sanitizeGCSError(err error) error {
	if err == nil {
		return nil
	}
	return ErrObjectNotFound
}
