package impl_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

func TestDevBuildPlatformField(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()

	t.Run("Create with platform field should persist platform", func(t *testing.T) {
		platform := "linux/amd64"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v9.0.0-test2",
				GitRef:  "branch/master",
				Platform: platform,
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.Equal(t, platform, build.Spec.Platform)
	})

	t.Run("Create with empty platform should persist empty", func(t *testing.T) {
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v9.0.0-test2",
				GitRef:  "branch/master",
				Platform: "",
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.Equal(t, "", build.Spec.Platform)
	})

	t.Run("Create with linux/arm64 platform should persist correctly", func(t *testing.T) {
		platform := "linux/arm64"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v9.0.0-test2",
				GitRef:  "branch/master",
				Platform: platform,
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.Equal(t, platform, build.Spec.Platform)
	})

	t.Run("Create and Get should return same platform", func(t *testing.T) {
		platform := "linux/amd64"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v9.0.0-test2",
				GitRef:  "branch/master",
				Platform: platform,
			},
			Dryrun: true,
		}

		created, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)

		getPayload := &devbuild.GetPayload{
			ID:   created.ID,
			Sync: false,
		}

		fetched, err := env.service.Get(ctx, getPayload)
		require.NoError(t, err)
		assert.Equal(t, platform, fetched.Spec.Platform)
	})
}
