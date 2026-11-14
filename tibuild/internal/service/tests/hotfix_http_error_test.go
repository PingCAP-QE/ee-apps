package impl_test

import (
	"context"
	"net/http"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
	hotfixsvr "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/hotfix/server"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// newHotfixHTTPHandler mounts the generated HTTP server on a real muxer. A
// service-level test cannot catch a missing Goa error mapping: the typed error
// is returned correctly, but the transport picks the status code from the Go
// type, so errors sharing one type all degrade to whichever status was
// registered first.
func newHotfixHTTPHandler(svc hotfix.Service) http.Handler {
	mux := goahttp.NewMuxer()
	srv := hotfixsvr.New(
		hotfix.NewEndpoints(svc),
		mux,
		goahttp.RequestDecoder,
		goahttp.ResponseEncoder,
		func(_ context.Context, w http.ResponseWriter, err error) {
			http.Error(w, err.Error(), http.StatusInternalServerError)
		},
		nil,
	)
	hotfixsvr.Mount(mux, srv)
	return mux
}

func TestHotfixHTTP_DeniedRepoIsForbidden(t *testing.T) {
	logger := zerolog.Nop()
	svc := impl.NewHotfix(&logger, nil, &config.Service{Hotfix: config.Hotfix{
		GithubRepoAllowlist: []string{"pingcap/pd"},
	}})
	handler := newHotfixHTTPHandler(svc)

	const target = "/api/v2/hotfix/bump-tag-for-tidbx"

	t.Run("repository outside the allowlist is 403", func(t *testing.T) {
		rec := postJSON(handler, target, `{"repo":"evil/fork","author":"http@test"}`)

		require.Equal(t, http.StatusForbidden, rec.Code, "body: %s", rec.Body.String())
		assert.Contains(t, rec.Body.String(), "evil/fork")
		assert.NotContains(t, rec.Body.String(), "pingcap/pd", "the response must not echo the allowlist")
	})

	t.Run("allowlisted repository passes the policy check", func(t *testing.T) {
		// No branch and no commit, so the request stops at the payload check
		// right after the allowlist instead of creating a tag.
		rec := postJSON(handler, target, `{"repo":"pingcap/pd","author":"http@test"}`)

		require.Equal(t, http.StatusBadRequest, rec.Code, "body: %s", rec.Body.String())
		assert.Contains(t, rec.Body.String(), "branch")
	})
}
