package impl

import (
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	tidbcloudsvr "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/tidbcloud/server"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

// newTidbcloudHTTPTestServer mounts the generated transport on a real HTTP
// server so validation statuses and error envelopes are exercised end to end.
func newTidbcloudHTTPTestServer(t *testing.T) *httptest.Server {
	t.Helper()

	svc := newTidbcloudTestService(t, nil, nil)
	mux := goahttp.NewMuxer()
	server := tidbcloudsvr.New(tidbcloud.NewEndpoints(svc), mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, nil, nil)
	tidbcloudsvr.Mount(mux, server)

	httpServer := httptest.NewServer(mux)
	t.Cleanup(httpServer.Close)
	return httpServer
}

func TestTidbcloudHTTPContract(t *testing.T) {
	server := newTidbcloudHTTPTestServer(t)

	t.Run("cloudconfig requires stage and image", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/devops/cloudconfig/versions/component", map[string]any{})
		require.Equal(t, 400, status)
		require.Equal(t, "missing_field", body["name"])
		require.Contains(t, body["message"], "stage")
		require.Contains(t, body["message"], "image")
	})

	t.Run("tcms requires image", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/tidbx-component-image-builds", map[string]any{})
		require.Equal(t, 400, status)
		require.Equal(t, "missing_field", body["name"])
		require.Contains(t, body["message"], "image")
	})

	t.Run("commit sha has a fixed length", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/tidbx-component-image-builds", map[string]any{
			"image": "a/b:c",
			"github": map[string]any{
				"full_repo":  "a/b",
				"commit_sha": "short",
			},
		})
		require.Equal(t, 400, status)
		require.Equal(t, "invalid_length", body["name"])
		require.Contains(t, body["message"], "commit_sha")
	})

	t.Run("sync kernel images requires stage and images", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/sync-kernel-images", map[string]any{})
		require.Equal(t, 400, status)
		require.Equal(t, "missing_field", body["name"])
		require.Contains(t, body["message"], "stage")
		require.Contains(t, body["message"], "images")
	})

	t.Run("sync kernel images stage enum", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/sync-kernel-images", map[string]any{
			"stage":  "staging",
			"images": []string{"us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"},
		})
		require.Equal(t, 400, status)
		require.Equal(t, "invalid_enum_value", body["name"])
		require.Contains(t, body["message"], "dev")
	})

	t.Run("sync kernel images pattern", func(t *testing.T) {
		status, body := doJSON(t, "POST", server.URL+"/api/v2/tidbcloud/sync-kernel-images", map[string]any{
			"stage":  "dev",
			"images": []string{"not an image"},
		})
		require.Equal(t, 400, status)
		require.Equal(t, "invalid_pattern", body["name"])
		require.Contains(t, body["message"], "images")
	})
}
