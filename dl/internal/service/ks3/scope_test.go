package ks3

import (
	"testing"

	"github.com/stretchr/testify/require"

	pkgks3 "github.com/PingCAP-QE/ee-apps/dl/pkg/ks3"
)

// The request names the bucket and the key, and the service reads them with its
// own credentials, so an unset allowlist has to deny rather than allow.
func TestScopeAllows(t *testing.T) {
	tests := []struct {
		name    string
		cfg     pkgks3.Config
		bucket  string
		key     string
		wantErr string
	}{
		{
			name:    "an unset allowlist denies every bucket",
			bucket:  "hotfix-binaries",
			key:     "a/b.tar.gz",
			wantErr: "is not in the KS3 allowlist",
		},
		{
			name:   "an allowlisted bucket passes",
			cfg:    pkgks3.Config{AllowedBuckets: []string{"hotfix-binaries"}},
			bucket: "hotfix-binaries",
			key:    "a/b.tar.gz",
		},
		{
			name:   "matching ignores case and surrounding space",
			cfg:    pkgks3.Config{AllowedBuckets: []string{" Hotfix-Binaries "}},
			bucket: "HOTFIX-BINARIES",
			key:    "a/b.tar.gz",
		},
		{
			name:    "another bucket is denied",
			cfg:     pkgks3.Config{AllowedBuckets: []string{"hotfix-binaries"}},
			bucket:  "other",
			key:     "a/b.tar.gz",
			wantErr: "is not in the KS3 allowlist",
		},
		{
			name:    "a blank bucket is denied",
			cfg:     pkgks3.Config{AllowedBuckets: []string{"hotfix-binaries"}},
			bucket:  "  ",
			key:     "a/b.tar.gz",
			wantErr: "bucket is required",
		},
		{
			name:    "a key outside the prefixes is denied",
			cfg:     pkgks3.Config{AllowedBuckets: []string{"hotfix-binaries"}, AllowedKeyPrefixes: []string{"tikv/"}},
			bucket:  "hotfix-binaries",
			key:     "tidb/x.tar.gz",
			wantErr: "outside the allowed KS3 key prefixes",
		},
		{
			name:   "a key under a prefix passes",
			cfg:    pkgks3.Config{AllowedBuckets: []string{"hotfix-binaries"}, AllowedKeyPrefixes: []string{"tikv/"}},
			bucket: "hotfix-binaries",
			key:    "tikv/x.tar.gz",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := newScope(tc.cfg).allows(tc.bucket, tc.key)
			if tc.wantErr == "" {
				require.NoError(t, err)
				return
			}
			require.ErrorContains(t, err, tc.wantErr)
		})
	}
}

// TLS is on unless the deployment says otherwise.
func TestConfigTLSIsOnByDefault(t *testing.T) {
	var cfg pkgks3.Config
	require.False(t, cfg.DisableSSL, "plaintext must be an explicit choice")

	cfg.DisableSSL = true
	require.True(t, cfg.DisableSSL)
}
