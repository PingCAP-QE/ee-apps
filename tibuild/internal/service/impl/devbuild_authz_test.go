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
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
)

func TestAuthorizeBuildMutation(t *testing.T) {
	logger := zerolog.Nop()
	s := &devbuildsrvc{logger: &logger}
	build := &ent.DevBuild{ID: 7, CreatedBy: "owner@pingcap.com"}

	t.Run("the owner may modify their own build", func(t *testing.T) {
		ctx := identity.WithUser(context.Background(), identity.User{Email: "OWNER@pingcap.com"})
		require.NoError(t, s.authorizeBuildMutation(ctx, build, "update"))
	})

	t.Run("a machine principal may modify any build", func(t *testing.T) {
		ctx := identity.WithUser(context.Background(), identity.User{
			Email: "devops-server@gcp-resilience-dev.iam.gserviceaccount.com",
		})
		require.NoError(t, s.authorizeBuildMutation(ctx, build, "update"))
	})

	t.Run("another person is forbidden", func(t *testing.T) {
		ctx := identity.WithUser(context.Background(), identity.User{Email: "someone@pingcap.com"})
		err := s.authorizeBuildMutation(ctx, build, "update")
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
		assert.Equal(t, http.StatusForbidden, forbidden.Code)
	})

	t.Run("no principal is refused instead of falling open", func(t *testing.T) {
		err := s.authorizeBuildMutation(context.Background(), build, "update")
		var unauthorized *devbuild.DevBuildUnauthorizedError
		require.ErrorAs(t, err, &unauthorized)
		assert.Equal(t, http.StatusUnauthorized, unauthorized.Code)
	})
}

func TestIsMachinePrincipal(t *testing.T) {
	for _, tc := range []struct {
		email string
		want  bool
	}{
		{"devops-server@gcp-resilience-dev.iam.gserviceaccount.com", true},
		{"tirelease-tibuild-client@pingcap-testing-account.iam.gserviceaccount.com", true},
		{"DEVOPS-SERVER@gcp-resilience-dev.IAM.GSERVICEACCOUNT.COM", true},
		{"alice@pingcap.com", false},
		{"", false},
		// A person whose address merely mentions the suffix is not a machine.
		{"iam.gserviceaccount.com@evil.example", false},
	} {
		assert.Equal(t, tc.want, isMachinePrincipal(tc.email), tc.email)
	}
}
