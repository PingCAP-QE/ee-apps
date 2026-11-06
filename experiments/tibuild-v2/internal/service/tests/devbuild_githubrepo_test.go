package impl_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func TestDevBuildGithubRepoField(t *testing.T) {
	// The allowlist has to cover the product default (pingcap/pd) and the
	// custom repository this test exercises.
	env := setupTestEnv(t, func(cfg *config.Service) {
		cfg.DevBuild.GithubRepoAllowlist = []string{"pingcap/pd", "tidbcloud/*"}
	})
	defer teardownTestEnv(env)

	ctx := context.Background()

	t.Run("Create with custom githubRepo persists it instead of product default", func(t *testing.T) {
		createPayload := &devbuild.CreatePayload{
			CreatedBy: stringPtr("test-user"),
			Request: &devbuild.DevBuildSpec{
				Product:    "pd",
				Edition:    "community",
				Version:    stringPtr("v9.0.0-test2"),
				GitRef:     "branch/master",
				GithubRepo: stringPtr("tidbcloud/cloud-storage-engine"),
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		require.NotNil(t, build)
		require.NotNil(t, build.Spec.GithubRepo)
		assert.Equal(t, "tidbcloud/cloud-storage-engine", *build.Spec.GithubRepo)
	})

	t.Run("Create without githubRepo falls back to product default", func(t *testing.T) {
		createPayload := &devbuild.CreatePayload{
			CreatedBy: stringPtr("test-user"),
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: stringPtr("v9.0.0-test2"),
				GitRef:  "branch/master",
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		require.NotNil(t, build)
		require.NotNil(t, build.Spec.GithubRepo)
		assert.Equal(t, "pingcap/pd", *build.Spec.GithubRepo)
	})

	t.Run("Create with a githubRepo outside the allowlist is forbidden", func(t *testing.T) {
		createPayload := &devbuild.CreatePayload{
			CreatedBy: stringPtr("test-user"),
			Request: &devbuild.DevBuildSpec{
				Product:    "pd",
				Edition:    "community",
				Version:    stringPtr("v9.0.0-test2"),
				GitRef:     "branch/master",
				GithubRepo: stringPtr("evil/fork"),
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.Error(t, err)
		assert.Nil(t, build)
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
		assert.Equal(t, 403, forbidden.Code)
		assert.Contains(t, forbidden.Message, "evil/fork")
	})
}
