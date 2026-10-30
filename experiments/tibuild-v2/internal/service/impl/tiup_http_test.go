package impl

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	publishsvr "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/publish/server"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
)

// newTiUPHTTPTestServer mounts the generated TiUP transport on a real HTTP
// server, so the status codes and the error envelopes are exercised end to end.
func newTiUPHTTPTestServer(t *testing.T) (*httptest.Server, *publishsrvc) {
	t.Helper()

	svc, _ := newFlowTestService(t)
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	cases := loadGoldenCases(t)
	svc.fetchConfig = func(ctx context.Context, artifactURL string) (map[string]any, string, error) {
		c, ok := cases[artifactURL]
		if !ok {
			return nil, "", os.ErrNotExist
		}
		return c.ArtifactConfig, "sha256:1111111111111111111111111111111111111111111111111111111111111111", nil
	}

	mux := goahttp.NewMuxer()
	server := publishsvr.New(publish.NewEndpoints(svc), mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, nil, nil)
	publishsvr.Mount(mux, server)

	httpServer := httptest.NewServer(mux)
	t.Cleanup(httpServer.Close)
	return httpServer, svc
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		require.NoError(t, err)
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, url, reader)
	require.NoError(t, err)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)

	var out map[string]any
	if len(raw) > 0 && raw[0] == '{' {
		require.NoError(t, json.Unmarshal(raw, &out))
	}
	return resp.StatusCode, out
}

// Validation failures keep Goa's default envelope, which is what the old
// publisher returned.
func TestTiUPHTTPValidationEnvelope(t *testing.T) {
	server, _ := newTiUPHTTPTestServer(t)

	status, body := doJSON(t, http.MethodPost, server.URL+"/api/v2/publish/tiup/delivery-by-rules", map[string]any{})
	require.Equal(t, http.StatusBadRequest, status)
	require.Equal(t, "missing_field", body["name"])
	require.Equal(t, false, body["fault"])
	require.Contains(t, body["message"], "artifact_url")

	status, body = doJSON(t, http.MethodPost, server.URL+"/api/v2/publish/tiup/publish-request", map[string]any{
		"artifact_url": "oci.com/repo:tag",
		"tiup_mirror":  "bad",
	})
	require.Equal(t, http.StatusBadRequest, status)
	require.Equal(t, "invalid_enum_value", body["name"])
	require.Equal(t, false, body["fault"])
}

// The legacy status route keeps the 500 + fault envelope for unknown ids.
func TestTiUPHTTPUnknownRequestIDIsFault(t *testing.T) {
	server, _ := newTiUPHTTPTestServer(t)

	status, body := doJSON(t, http.MethodGet,
		server.URL+"/api/v2/publish/tiup/publish-request/00000000-0000-4000-8000-000000000000", nil)
	require.Equal(t, http.StatusInternalServerError, status)
	require.Equal(t, "fault", body["name"])
	require.Equal(t, true, body["fault"])
	require.Contains(t, body["message"], "request ID not found")
}

// delivery-by-rules returns one id per target and keeps the legacy body shape.
func TestTiUPHTTPDeliveryByRules(t *testing.T) {
	server, _ := newTiUPHTTPTestServer(t)

	status, _ := doJSON(t, http.MethodPost, server.URL+"/api/v2/publish/tiup/delivery-by-rules", map[string]any{
		"artifact_url": nightlyArtifact,
	})
	require.Equal(t, http.StatusOK, status)

	// The response is a JSON array, so decode it separately.
	raw, err := json.Marshal(map[string]any{})
	require.NoError(t, err)
	_ = raw

	resp, err := http.Post(server.URL+"/api/v2/publish/tiup/delivery-by-rules", "application/json",
		bytes.NewReader([]byte(`{"artifact_url":"`+nightlyArtifact+`"}`)))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusOK, resp.StatusCode)

	var ids []string
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&ids))
	require.Len(t, ids, 2)
}

// The new unit route answers 404 with the NotFound error type (not a 500).
func TestTiUPHTTPGetUnitNotFound(t *testing.T) {
	server, _ := newTiUPHTTPTestServer(t)

	status, body := doJSON(t, http.MethodGet, server.URL+"/api/v2/publish/units/missing", nil)
	require.Equal(t, http.StatusNotFound, status)
	require.Contains(t, body, "message")
}
