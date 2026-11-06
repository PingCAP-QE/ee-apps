package impl

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestValidateTiUPHTTPPublishSource(t *testing.T) {
	tests := []struct {
		name    string
		allowed []string
		url     string
		wantErr string
	}{
		{
			name:    "empty allowlist rejects every http source",
			allowed: nil,
			url:     "https://objects.githubusercontent.com/build.tar.gz",
			wantErr: "http publish sources are disabled",
		},
		{
			name:    "host outside the allowlist is rejected",
			allowed: []string{"objects.githubusercontent.com"},
			url:     "https://attacker.example/build.tar.gz",
			wantErr: "is not in publish.http_source_allowed_hosts",
		},
		{
			name:    "exact allowlisted host passes",
			allowed: []string{"objects.githubusercontent.com"},
			url:     "https://objects.githubusercontent.com/build.tar.gz",
		},
		{
			name:    "https and http both pass for an allowlisted host",
			allowed: []string{"artifacts.internal"},
			url:     "http://artifacts.internal/pkg.tar.gz",
		},
		{
			name:    "port is ignored when matching",
			allowed: []string{"artifacts.internal"},
			url:     "https://artifacts.internal:8443/pkg.tar.gz",
		},
		{
			name:    "allowlist entries are case and whitespace insensitive",
			allowed: []string{"  Objects.GitHubUserContent.com  "},
			url:     "https://objects.githubusercontent.com/build.tar.gz",
		},
		{
			name:    "subdomain of an exact entry does not pass",
			allowed: []string{"example.com"},
			url:     "https://evil.example.com/build.tar.gz",
			wantErr: "is not in publish.http_source_allowed_hosts",
		},
		{
			name:    "wildcard entry allows subdomains",
			allowed: []string{"*.example.com"},
			url:     "https://cdn.eu.example.com/build.tar.gz",
		},
		{
			name:    "wildcard entry does not allow the bare domain",
			allowed: []string{"*.example.com"},
			url:     "https://example.com/build.tar.gz",
			wantErr: "is not in publish.http_source_allowed_hosts",
		},
		{
			name:    "look-alike suffix is not matched",
			allowed: []string{"example.com", "*.example.org"},
			url:     "https://evilexample.com/build.tar.gz",
			wantErr: "is not in publish.http_source_allowed_hosts",
		},
		{
			name:    "non-http scheme is rejected",
			allowed: []string{"example.com"},
			url:     "ftp://example.com/build.tar.gz",
			wantErr: "scheme \"ftp\" is not allowed",
		},
		{
			name:    "file scheme is rejected",
			allowed: []string{"localhost"},
			url:     "file:///etc/passwd",
			wantErr: `scheme "file" is not allowed`,
		},
		{
			name:    "credentials in the url are rejected",
			allowed: []string{"example.com"},
			url:     "https://user:pass@example.com/build.tar.gz",
			wantErr: "must not carry credentials",
		},
		{
			name:    "empty url is rejected",
			allowed: []string{"example.com"},
			url:     "   ",
			wantErr: "from.http.url is required",
		},
		{
			name:    "url without a host is rejected",
			allowed: []string{"example.com"},
			url:     "https:///build.tar.gz",
			wantErr: "has no host",
		},
		{
			name:    "padded url is accepted",
			allowed: []string{"example.com"},
			url:     "  https://example.com/build.tar.gz\n",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateTiUPHTTPPublishSource(tc.allowed, tc.url)
			if tc.wantErr == "" {
				require.NoError(t, err)
				return
			}
			require.Error(t, err)
			require.Contains(t, err.Error(), tc.wantErr)
		})
	}
}
