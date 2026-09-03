package impl_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
)

func TestPortalDevBuildIdentityAndCapabilities(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	aliceCtx := identity.WithUser(context.Background(), identity.User{Email: "alice@pingcap.com"})
	forged := "mallory@pingcap.com"
	created, err := env.service.Create(aliceCtx, &devbuild.CreatePayload{
		CreatedBy: &forged,
		Request: &devbuild.DevBuildSpec{
			Product:  "pd",
			Edition:  "community",
			GitRef:   "branch/master",
			Platform: "linux/amd64",
		},
		Dryrun: true,
	})
	require.NoError(t, err)
	assert.Equal(t, "alice@pingcap.com", created.Meta.CreatedBy)
	assert.Nil(t, created.Spec.Version)

	bobCtx := identity.WithUser(context.Background(), identity.User{Email: "bob@pingcap.com"})
	_, err = env.service.Create(bobCtx, &devbuild.CreatePayload{
		Request: &devbuild.DevBuildSpec{
			Product: "pd", Edition: "community", GitRef: "tag/v8.5.0", Platform: "linux",
		},
		Dryrun: true,
	})
	require.NoError(t, err)

	mine, err := env.service.List(aliceCtx, &devbuild.ListPayload{
		Page: 1, PageSize: 20, Scope: "mine", Sort: "createdAt", Direction: "desc",
	})
	require.NoError(t, err)
	require.Len(t, mine, 1)
	assert.Equal(t, "alice@pingcap.com", mine[0].Meta.CreatedBy)

	status := devbuild.BuildStatus("SUCCESS")
	_, err = env.service.Update(aliceCtx, &devbuild.UpdatePayload{
		ID: created.ID, Status: &devbuild.DevBuildStatus{Status: status},
	})
	require.NoError(t, err)
	owned, err := env.service.Get(aliceCtx, &devbuild.GetPayload{ID: created.ID})
	require.NoError(t, err)
	require.NotNil(t, owned.Permissions)
	assert.True(t, owned.Permissions.CanRerun)

	notOwned, err := env.service.Get(bobCtx, &devbuild.GetPayload{ID: created.ID})
	require.NoError(t, err)
	assert.False(t, notOwned.Permissions.CanRerun)
	_, err = env.service.Rerun(bobCtx, &devbuild.RerunPayload{ID: created.ID})
	var forbidden *devbuild.DevBuildForbiddenError
	require.ErrorAs(t, err, &forbidden)
	assert.Equal(t, "Forbidden", forbidden.GoaErrorName())

	capabilities, err := env.service.Capabilities(context.Background())
	require.NoError(t, err)
	require.Len(t, capabilities.Products, 1)
	assert.Equal(t, "pd", capabilities.Products[0].ID)
	assert.Equal(t, []string{"tekton"}, capabilities.PipelineEngines)
}

func TestPortalDevBuildRejectsInvalidGitRef(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := identity.WithUser(context.Background(), identity.User{Email: "alice@pingcap.com"})
	_, err := env.service.Create(ctx, &devbuild.CreatePayload{
		Request: &devbuild.DevBuildSpec{
			Product: "pd", Edition: "community", GitRef: "master", Platform: "linux",
		},
		Dryrun: true,
	})
	var badRequest *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &badRequest)
	assert.Equal(t, "BadRequest", badRequest.GoaErrorName())
}
