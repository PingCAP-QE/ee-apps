package config

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestNewBuilderImageAllowlist_AcceptsNamespaces(t *testing.T) {
	allow, err := NewBuilderImageAllowlist([]string{
		"ghcr.io/pingcap-qe",
		// A trailing slash and upper case are accepted, because the value a
		// human writes in YAML does not need to be a normalised reference.
		"hub.pingcap.net/pingcap/",
		"US-DOCKER.PKG.DEV/pingcap-testing-account",
	})
	require.NoError(t, err)

	require.Equal(t, []string{
		"ghcr.io/pingcap-qe",
		"hub.pingcap.net/pingcap",
		"us-docker.pkg.dev/pingcap-testing-account",
	}, allow.Entries())

	require.True(t, allow.Allows("ghcr.io/pingcap-qe/cd/utils/release"))
	require.True(t, allow.Allows("ghcr.io/pingcap-qe"))
	require.True(t, allow.Allows("us-docker.pkg.dev/pingcap-testing-account/hotfix/x"))

	// Whole path segments only: a longer namespace that merely shares the
	// prefix must not match.
	require.False(t, allow.Allows("ghcr.io/pingcap-qe-evil/x"))
	require.False(t, allow.Allows("docker.io/pingcap-qe/cd"))
}

func TestNewBuilderImageAllowlist_RejectsEntriesThatCannotMatch(t *testing.T) {
	tests := []struct {
		name  string
		entry string
	}{
		{name: "empty entry", entry: "  "},
		{name: "bare organisation name", entry: "pingcap"},
		{name: "tag", entry: "ghcr.io/pingcap-qe/cd/utils/release:v1"},
		{name: "digest", entry: "ghcr.io/pingcap-qe/cd/utils/release@sha256:0a8970b3866031092bc9dcf5ff14e0101c6cc204b624ca4cddaafdda31807985"},
		{name: "wildcard", entry: "ghcr.io/pingcap-qe/*"},
		{name: "invalid characters", entry: "ghcr.io/pingcap qe"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewBuilderImageAllowlist([]string{tc.entry})
			require.Error(t, err)
		})
	}
}

func TestNewBuilderImageAllowlist_EmptyAllowsNothing(t *testing.T) {
	allow, err := NewBuilderImageAllowlist(nil)
	require.NoError(t, err)
	require.True(t, allow.Empty())
	require.False(t, allow.Allows("ghcr.io/pingcap-qe/cd/utils/release"))
}

func TestDevBuildValidate_RejectsInvalidBuilderImageEntries(t *testing.T) {
	err := DevBuild{BuilderImageAllowedRepositories: []string{"ghcr.io/pingcap-qe/cd:latest"}}.
		Validate(map[string]string{"tidb": "pingcap/tidb"})
	require.Error(t, err)
	require.Contains(t, err.Error(), "devbuild.builder_image_allowed_repositories")

	require.NoError(t, DevBuild{
		BuilderImageAllowedRepositories: []string{"ghcr.io/pingcap-qe"},
		GithubRepoAllowlist:             []string{"pingcap/*"},
	}.Validate(map[string]string{"tidb": "pingcap/tidb"}))
}
