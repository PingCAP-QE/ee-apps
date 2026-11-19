package impl

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// An OCI publish source pins what is published but not where it is read from,
// so the registry host needs the same fail-closed allowlist as an http source.
func TestValidateTiUPOCIPublishSource(t *testing.T) {
	tests := []struct {
		name    string
		allowed []string
		source  string
		wantErr string
	}{
		{
			name:    "an empty allowlist disables the source",
			source:  "hub.pingcap.net/pingcap/tidb:v8.5.0",
			wantErr: "oci publish sources are disabled",
		},
		{
			name:    "an exact host passes",
			allowed: []string{"hub.pingcap.net"},
			source:  "hub.pingcap.net/pingcap/tidb:v8.5.0",
		},
		{
			name:    "matching ignores case and the tag",
			allowed: []string{"HUB.pingcap.net"},
			source:  "hub.pingcap.net/pingcap/tidb@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		},
		{
			name:    "a wildcard covers subdomains",
			allowed: []string{"*.example.com"},
			source:  "registry.example.com/team/app:v1.0.0",
		},
		{
			name:    "a wildcard does not cover the bare domain",
			allowed: []string{"*.example.com"},
			source:  "example.com/team/app:v1.0.0",
			wantErr: "is not in publish.oci_source_allowed_hosts",
		},
		{
			name:    "a wildcard does not cover a look-alike host",
			allowed: []string{"*.example.com"},
			source:  "evilexample.com/team/app:v1.0.0",
			wantErr: "is not in publish.oci_source_allowed_hosts",
		},
		{
			name:    "another registry is refused",
			allowed: []string{"hub.pingcap.net"},
			source:  "evil.example.com/pingcap/tidb:v8.5.0",
			wantErr: "is not in publish.oci_source_allowed_hosts",
		},
		{
			name:    "a bare name normalizes to docker.io",
			allowed: []string{"docker.io"},
			source:  "library/alpine:latest",
		},
		{
			name:    "a URL is not an OCI reference",
			allowed: []string{"hub.pingcap.net"},
			source:  "https://hub.pingcap.net/pingcap/tidb:v8.5.0",
			wantErr: "must be an OCI reference",
		},
		{
			name:    "an empty source is refused",
			allowed: []string{"hub.pingcap.net"},
			source:  "  ",
			wantErr: "artifact_url is required",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateTiUPOCIPublishSource(tc.allowed, tc.source)
			if tc.wantErr == "" {
				require.NoError(t, err)
				return
			}
			require.ErrorContains(t, err, tc.wantErr)
		})
	}
}
