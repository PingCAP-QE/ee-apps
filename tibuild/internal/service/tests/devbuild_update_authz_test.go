package impl_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
)

func TestDevBuildUpdateAuthorization(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	alice := identity.WithUser(context.Background(), identity.User{Email: "alice@pingcap.com"})
	created, err := env.service.Create(alice, &devbuild.CreatePayload{
		Request: &devbuild.DevBuildSpec{
			Product: "pd", Edition: "community", GitRef: "branch/master", Platform: "linux/amd64",
		},
		Dryrun: true,
	})
	require.NoError(t, err)

	update := &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: devbuild.BuildStatus("PROCESSING")},
	}

	// A different person may not rewrite somebody else's build record.
	bob := identity.WithUser(context.Background(), identity.User{Email: "bob@pingcap.com"})
	_, err = env.service.Update(bob, update)
	var forbidden *devbuild.DevBuildForbiddenError
	require.ErrorAs(t, err, &forbidden)
	assert.Equal(t, "Forbidden", forbidden.GoaErrorName())

	// A request that carries no principal did not come through the Gateway.
	_, err = env.service.Update(context.Background(), update)
	var unauthorized *devbuild.DevBuildUnauthorizedError
	require.ErrorAs(t, err, &unauthorized)
	assert.Equal(t, "Unauthorized", unauthorized.GoaErrorName())

	// The creator may update their own build.
	_, err = env.service.Update(alice, update)
	require.NoError(t, err)

	// So may the build automation, which records builds on behalf of a person.
	machine := identity.WithUser(context.Background(), identity.User{
		Email: "devops-server@gcp-resilience-dev.iam.gserviceaccount.com",
	})
	_, err = env.service.Update(machine, update)
	require.NoError(t, err)
}

func TestDevBuildUpdateRejectsTerminalStatusChange(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	alice := identity.WithUser(context.Background(), identity.User{Email: "alice@pingcap.com"})
	created, err := env.service.Create(alice, &devbuild.CreatePayload{
		Request: &devbuild.DevBuildSpec{
			Product: "pd", Edition: "community", GitRef: "branch/master", Platform: "linux/amd64",
		},
		Dryrun: true,
	})
	require.NoError(t, err)

	finished := &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: devbuild.BuildStatus("SUCCESS")},
	}
	_, err = env.service.Update(alice, finished)
	require.NoError(t, err)

	// Moving a finished build back out of a terminal state would also re-open
	// the rerun precondition.
	_, err = env.service.Update(alice, &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: devbuild.BuildStatus("PROCESSING")},
	})
	var badRequest *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &badRequest)

	// Repeating the same terminal status stays idempotent.
	_, err = env.service.Update(alice, finished)
	require.NoError(t, err)
}
