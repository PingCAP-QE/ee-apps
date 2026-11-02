package gcs

import (
	"errors"
	"testing"
)

func TestValidateBucket(t *testing.T) {
	allowed := []string{"hotfix-binaries"}

	tests := []struct {
		name    string
		bucket  string
		wantErr bool
	}{
		{name: "allowed", bucket: "hotfix-binaries"},
		{name: "allowed uppercase", bucket: "HOTFIX-BINARIES"},
		{name: "not allowed", bucket: "prow-tidb-logs", wantErr: true},
		{name: "empty", bucket: "", wantErr: true},
		{name: "wildcard", bucket: "*", wantErr: true},
		{name: "path", bucket: "hotfix-binaries/../other", wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateBucket(tt.bucket, allowed)
			if tt.wantErr && !errors.Is(err, ErrTargetNotAllowed) {
				t.Fatalf("ValidateBucket(%q) error = %v, want ErrTargetNotAllowed", tt.bucket, err)
			}
			if !tt.wantErr && err != nil {
				t.Fatalf("ValidateBucket(%q) unexpected error: %v", tt.bucket, err)
			}
		})
	}
}

func TestValidateObjectKey(t *testing.T) {
	tests := []struct {
		name     string
		key      string
		prefixes []string
		wantErr  bool
	}{
		{name: "no prefix restriction", key: "anything/x", prefixes: nil},
		{name: "matching prefix", key: "releases/v1/x", prefixes: []string{"releases/"}},
		{name: "non-matching prefix", key: "secrets/x", prefixes: []string{"releases/"}, wantErr: true},
		{name: "empty key", key: "", prefixes: nil, wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateObjectKey(tt.key, tt.prefixes)
			if tt.wantErr && !errors.Is(err, ErrInvalidObjectKey) {
				t.Fatalf("ValidateObjectKey(%q) error = %v, want ErrInvalidObjectKey", tt.key, err)
			}
			if !tt.wantErr && err != nil {
				t.Fatalf("ValidateObjectKey(%q) unexpected error: %v", tt.key, err)
			}
		})
	}
}

func TestSanitizeGCSError(t *testing.T) {
	raw := errors.New("storage: object doesn't exist: hotfix-binaries-reader@pingcap-testing-account.iam.gserviceaccount.com")
	got := sanitizeGCSError(raw).Error()
	if got == raw.Error() {
		t.Fatal("sanitizeGCSError returned the upstream error")
	}
	if contains(got, "@") {
		t.Fatalf("sanitizeGCSError leaked an identity: %q", got)
	}
	if sanitizeGCSError(nil) != nil {
		t.Fatal("sanitizeGCSError(nil) should return nil")
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
