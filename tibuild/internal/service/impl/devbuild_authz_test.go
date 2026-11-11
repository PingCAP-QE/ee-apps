package impl

import (
	"context"
	"net/http"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/authz"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
)

func TestAuthorizeBuildMutation(t *testing.T) {
	logger := zerolog.Nop()
	build := &ent.DevBuild{ID: 7, CreatedBy: "owner@pingcap.com"}

	newService := func(mode authz.Mode) *devbuildsrvc {
		return &devbuildsrvc{logger: &logger, authorizationMode: string(mode)}
	}

	t.Run("automation may act on any build", func(t *testing.T) {
		s := newService(authz.ModeEnforce)
		ctx := identity.WithUser(context.Background(), identity.User{
			Email:   "tibuild-v2@pingcap.com",
			Service: true,
		})
		require.NoError(t, s.authorizeBuildMutation(ctx, build, "rerun"))
	})

	t.Run("the owner may act on their own build", func(t *testing.T) {
		s := newService(authz.ModeEnforce)
		ctx := identity.WithUser(context.Background(), identity.User{Email: "OWNER@pingcap.com"})
		require.NoError(t, s.authorizeBuildMutation(ctx, build, "update"))
	})

	t.Run("a different person is forbidden", func(t *testing.T) {
		s := newService(authz.ModeEnforce)
		ctx := identity.WithUser(context.Background(), identity.User{Email: "someone@pingcap.com"})
		err := s.authorizeBuildMutation(ctx, build, "rerun")
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
		assert.Equal(t, http.StatusForbidden, forbidden.Code)
	})

	t.Run("a missing principal is refused once enforced", func(t *testing.T) {
		s := newService(authz.ModeEnforce)
		err := s.authorizeBuildMutation(context.Background(), build, "rerun")
		var unauthorized *devbuild.DevBuildUnauthorizedError
		require.ErrorAs(t, err, &unauthorized)
		assert.Equal(t, http.StatusUnauthorized, unauthorized.Code)
	})

	t.Run("a missing principal is only recorded before enforcement", func(t *testing.T) {
		for _, mode := range []authz.Mode{authz.ModeOff, authz.ModeObserve} {
			s := newService(mode)
			require.NoError(t, s.authorizeBuildMutation(context.Background(), build, "rerun"),
				"mode %s must keep the request working during rollout", mode)
		}
	})
}

// A caller that names a different creator must not pass: that is the fail-open
// condition the old `ok && !EqualFold(...)` check had.
func TestAuthorizeBuildMutationIgnoresClaimedCreator(t *testing.T) {
	logger := zerolog.Nop()
	s := &devbuildsrvc{logger: &logger, authorizationMode: string(authz.ModeEnforce)}
	build := &ent.DevBuild{ID: 9, CreatedBy: "owner@pingcap.com"}

	// The principal is who authenticated; a createdBy in the payload is not an
	// input to this decision at all.
	ctx := identity.WithUser(context.Background(), identity.User{Email: "attacker@pingcap.com"})
	var forbidden *devbuild.DevBuildForbiddenError
	require.ErrorAs(t, s.authorizeBuildMutation(ctx, build, "update"), &forbidden)
}
