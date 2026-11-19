package impl

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// TestRerunValidatesStoredBuilderImage covers the second surface that hands a
// builder image to the build step: a rerun replays the value stored on the
// record, which may predate the allowlist.
func TestRerunValidatesStoredBuilderImage(t *testing.T) {
	tests := []struct {
		name       string
		allow      []string
		builderImg string
		wantErr    bool
	}{
		{
			name:       "an unconfigured allowlist rejects a stored override",
			builderImg: "example.com/anything:v1",
			wantErr:    true,
		},
		{
			name:       "a repository outside the allowlist cannot be rerun",
			allow:      []string{"ghcr.io/pingcap-qe"},
			builderImg: "example.com/anything:v1",
			wantErr:    true,
		},
		{
			name:       "a repository inside the allowlist reruns",
			allow:      []string{"ghcr.io/pingcap-qe"},
			builderImg: "ghcr.io/pingcap-qe/cd/builders/tidb:v1",
		},
		{
			name: "a record without an override reruns",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := newAllowlistTestEnv(t, []string{"pingcap/pd"}, nil)
			env.service.builderImageAllowlist = tc.allow
			seed := env.db.DevBuild.Create().
				SetProduct("pd").
				SetVersion("v1.0.0").
				SetGithubRepo("pingcap/pd").
				SetGitRef("branch/main").
				SetGitHash(allowlistTestSHA).
				SetPlatform("linux/amd64").
				SetBuilderImg(tc.builderImg).
				SetStatus("SUCCESS").
				SetCreatedBy("tester@example.com").
				SetCreatedAt(time.Now()).
				SaveX(context.Background())

			before := env.count(t)
			record, err := env.service.Rerun(context.Background(), &devbuild.RerunPayload{ID: seed.ID})
			if tc.wantErr {
				require.Error(t, err)
				var forbidden *devbuild.DevBuildForbiddenError
				require.ErrorAs(t, err, &forbidden)
				require.Equal(t, http.StatusForbidden, forbidden.Code)
				require.Equal(t, before, env.count(t), "a rejected rerun must not store a build")
				require.Equal(t, 0, env.events(), "a rejected rerun must not send a CloudEvent")
				return
			}
			require.NoError(t, err)
			require.Equal(t, tc.builderImg, derefString(record.Spec.BuilderImg))
			require.Equal(t, before+1, env.count(t))
			require.Equal(t, 1, env.events())
		})
	}
}
