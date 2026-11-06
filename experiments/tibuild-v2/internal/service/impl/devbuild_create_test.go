package impl

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func TestResolveGithubRepo(t *testing.T) {
	productRepoMap := map[string]string{
		"tikv": "tikv/tikv",
		"pd":   "pingcap/pd",
	}
	allowCSE, err := config.NewRepoAllowlist([]string{"tidbcloud/*"})
	require.NoError(t, err)
	var emptyAllow config.RepoAllowlist

	t.Run("explicit repo wins over product default", func(t *testing.T) {
		repo, err := resolveGithubRepo("tidbcloud/cloud-storage-engine", productRepoMap, "tikv", allowCSE)
		require.NoError(t, err)
		require.Equal(t, "tidbcloud/cloud-storage-engine", repo)
	})

	t.Run("empty explicit repo falls back to product default", func(t *testing.T) {
		repo, err := resolveGithubRepo("", productRepoMap, "tikv", emptyAllow)
		require.NoError(t, err)
		require.Equal(t, "tikv/tikv", repo)
	})

	t.Run("unknown product without explicit repo is rejected", func(t *testing.T) {
		_, err := resolveGithubRepo("", productRepoMap, "cloud-storage-engine", emptyAllow)
		require.Error(t, err)
		var badReq *devbuild.DevBuildBadRequestError
		require.ErrorAs(t, err, &badReq)
		require.Contains(t, badReq.Message, "unknown product")
	})

	t.Run("repo without owner/repo form is rejected", func(t *testing.T) {
		_, err := resolveGithubRepo("tidbcloud/cloud-storage-engine/extra", productRepoMap, "tikv", allowCSE)
		require.Error(t, err)
		var badReq *devbuild.DevBuildBadRequestError
		require.ErrorAs(t, err, &badReq)
		require.Contains(t, badReq.Message, "<owner>/<repo>")
	})

	t.Run("repo with empty owner or repo name is rejected", func(t *testing.T) {
		for _, repo := range []string{"/tikv", "tikv/", "tikv//tikv"} {
			_, err := resolveGithubRepo(repo, productRepoMap, "tikv", allowCSE)
			require.Error(t, err, "repo %q should be rejected", repo)
			var badReq *devbuild.DevBuildBadRequestError
			require.ErrorAs(t, err, &badReq)
			require.Contains(t, badReq.Message, "<owner>/<repo>")
		}
	})

	t.Run("an explicit repo outside the allowlist is forbidden", func(t *testing.T) {
		_, err := resolveGithubRepo("evil/fork", productRepoMap, "tikv", allowCSE)
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
		require.Equal(t, 403, forbidden.Code)
	})

	t.Run("an empty allowlist accepts only the product default", func(t *testing.T) {
		repo, err := resolveGithubRepo("tikv/tikv", productRepoMap, "tikv", emptyAllow)
		require.NoError(t, err)
		require.Equal(t, "tikv/tikv", repo)

		_, err = resolveGithubRepo("evil/fork", productRepoMap, "tikv", emptyAllow)
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
	})
}
