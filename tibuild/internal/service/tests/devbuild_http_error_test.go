package impl_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	devbuildsvr "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/devbuild/server"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// newDevbuildHTTPHandler mounts the generated HTTP server on a real muxer so
// the tests below observe the status code a client would see. Service-level
// tests cannot catch a missing Goa error mapping: the typed error is returned
// correctly, but the transport layer degrades an unmapped error to 500.
func newDevbuildHTTPHandler(svc devbuild.Service) http.Handler {
	mux := goahttp.NewMuxer()
	srv := devbuildsvr.New(
		devbuild.NewEndpoints(svc),
		mux,
		goahttp.RequestDecoder,
		goahttp.ResponseEncoder,
		func(_ context.Context, w http.ResponseWriter, err error) {
			http.Error(w, err.Error(), http.StatusInternalServerError)
		},
		nil,
	)
	devbuildsvr.Mount(mux, srv)
	return mux
}

func postJSON(handler http.Handler, target, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	var req *http.Request
	if body == "" {
		req = httptest.NewRequest(http.MethodPost, target, nil)
	} else {
		req = httptest.NewRequest(http.MethodPost, target, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	}
	handler.ServeHTTP(rec, req)
	return rec
}

func TestDevbuildHTTP_DeniedRepoIsForbidden(t *testing.T) {
	env := setupTestEnv(t, func(cfg *config.Service) {
		cfg.DevBuild.GithubRepoAllowlist = []string{"pingcap/pd"}
	})
	defer teardownTestEnv(env)
	handler := newDevbuildHTTPHandler(env.service)

	const creator = "http-deny@test"
	body := `{"createdBy":"` + creator + `","request":{"product":"pd","edition":"community","gitRef":"branch/master","githubRepo":"evil/fork"}}`

	t.Run("create rejects a repository outside the allowlist with 403", func(t *testing.T) {
		rec := postJSON(handler, "/api/v2/devbuilds", body)

		require.Equal(t, http.StatusForbidden, rec.Code, "body: %s", rec.Body.String())
		assert.Contains(t, rec.Body.String(), "evil/fork")
		assert.NotContains(t, rec.Body.String(), "pingcap/pd", "the response must not echo the allowlist")
	})

	t.Run("dry run rejects a repository outside the allowlist with 403", func(t *testing.T) {
		rec := postJSON(handler, "/api/v2/devbuilds?dryrun=true", body)

		require.Equal(t, http.StatusForbidden, rec.Code, "body: %s", rec.Body.String())
		assert.Contains(t, rec.Body.String(), "evil/fork")
	})

	assert.Equal(t, 0, countBuildsByCreator(t, env, creator), "a rejected request must not persist a devbuild")

	t.Run("create accepts an allowlisted repository", func(t *testing.T) {
		allowed := `{"createdBy":"` + creator + `","request":{"product":"pd","edition":"community","gitRef":"branch/master","githubRepo":"pingcap/pd"}}`
		rec := postJSON(handler, "/api/v2/devbuilds?dryrun=true", allowed)

		require.Equal(t, http.StatusOK, rec.Code, "body: %s", rec.Body.String())
		assert.Contains(t, rec.Body.String(), "pingcap/pd")
	})
}

func TestDevbuildHTTP_DeniedRerunIsForbidden(t *testing.T) {
	env := setupTestEnv(t, func(cfg *config.Service) {
		cfg.DevBuild.GithubRepoAllowlist = []string{"pingcap/pd", "tidbcloud/*"}
	})
	defer teardownTestEnv(env)

	// A record created while its repository was still allowlisted.
	created, err := env.service.Create(context.Background(), &devbuild.CreatePayload{
		CreatedBy: stringPtr("http-rerun@test"),
		Request: &devbuild.DevBuildSpec{
			Product:    "pd",
			Edition:    "community",
			Version:    stringPtr("v9.0.0-test-http"),
			GitRef:     "branch/master",
			GithubRepo: stringPtr("tidbcloud/cloud-storage-engine"),
		},
		Dryrun: true,
	})
	require.NoError(t, err)

	// Only terminal builds can be rerun; the deny check must be reached instead
	// of the earlier "not terminal" guard.
	terminal := devbuild.BuildStatus("FAILURE")
	_, err = env.service.Update(context.Background(), &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: terminal},
	})
	require.NoError(t, err)

	// The repository is removed from the allowlist afterwards.
	env.cfg.DevBuild.GithubRepoAllowlist = []string{"pingcap/pd"}
	env.service.(interface{ Reload(*config.Service) }).Reload(env.cfg)

	handler := newDevbuildHTTPHandler(env.service)
	before := countBuildsByCreator(t, env, "http-rerun@test")

	rec := postJSON(handler, "/api/v2/devbuilds/"+strconv.Itoa(created.ID)+"/rerun?dryrun=true", "")

	require.Equal(t, http.StatusForbidden, rec.Code, "body: %s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "tidbcloud/cloud-storage-engine")
	assert.Equal(t, before, countBuildsByCreator(t, env, "http-rerun@test"), "a rejected rerun must not persist a devbuild")
}
