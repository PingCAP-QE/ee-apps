package impl

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/publish/server"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
)

// newPublishTestServer mounts the generated HTTP transport on top of the
// service, so the status codes and the JSON field names are verified too.
func newPublishTestServer(t *testing.T, svc *publishsrvc) *httptest.Server {
	t.Helper()
	endpoints := publish.NewEndpoints(svc)
	mux := goahttp.NewMuxer()
	server.Mount(mux, server.New(endpoints, mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, nil, nil))
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return ts
}

func TestPublishHTTPAPI(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)
	ts := newPublishTestServer(t, svc)

	// Plan a delivery.
	resp, err := http.Post(ts.URL+"/api/v2/publish/images", "application/json",
		bytes.NewBufferString(`{"artifactUrl":"example.com/hub/pingcap/widget/image:v8.5.0"}`))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusAccepted, resp.StatusCode)

	var created struct {
		ID          string `json:"id"`
		Kind        string `json:"kind"`
		ArtifactURL string `json:"artifactUrl"`
		Digest      string `json:"digest"`
		State       string `json:"state"`
		Steps       []struct {
			ID     string `json:"id"`
			Target string `json:"target"`
			Dest   string `json:"dest"`
			State  string `json:"state"`
		} `json:"steps"`
	}
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&created))
	require.NotEmpty(t, created.ID)
	require.Equal(t, PublishKindImageCopy, created.Kind)
	require.Equal(t, "example.com/hub/pingcap/widget/image:v8.5.0", created.ArtifactURL)
	require.Equal(t, publishTestDigest, created.Digest)
	require.Equal(t, PublishStateQueued, created.State)
	require.Len(t, created.Steps, 2)
	require.Equal(t, "docker.io", created.Steps[0].Target)
	require.Equal(t, "docker.io/pingcap/widget", created.Steps[0].Dest)
	require.Equal(t, PublishStepStatePending, created.Steps[0].State)

	// Read it back.
	getResp, err := http.Get(ts.URL + "/api/v2/publish/units/" + created.ID)
	require.NoError(t, err)
	defer getResp.Body.Close()
	require.Equal(t, http.StatusOK, getResp.StatusCode)

	// List it.
	listResp, err := http.Get(ts.URL + "/api/v2/publish/units?status=queued")
	require.NoError(t, err)
	defer listResp.Body.Close()
	require.Equal(t, http.StatusOK, listResp.StatusCode)
	var units []struct {
		ID string `json:"id"`
	}
	require.NoError(t, json.NewDecoder(listResp.Body).Decode(&units))
	require.Len(t, units, 1)
	require.Equal(t, created.ID, units[0].ID)

	// Nothing failed yet, so a retry is a client error, not a silent no-op.
	retryResp, err := http.Post(ts.URL+"/api/v2/publish/units/"+created.ID+"/retry", "application/json", nil)
	require.NoError(t, err)
	defer retryResp.Body.Close()
	require.Equal(t, http.StatusBadRequest, retryResp.StatusCode)

	// Unknown unit.
	missingResp, err := http.Get(ts.URL + "/api/v2/publish/units/missing")
	require.NoError(t, err)
	defer missingResp.Body.Close()
	require.Equal(t, http.StatusNotFound, missingResp.StatusCode)

	// Malformed reference.
	badResp, err := http.Post(ts.URL+"/api/v2/publish/images", "application/json",
		bytes.NewBufferString(`{"artifactUrl":"example.com/hub/pingcap/widget/image"}`))
	require.NoError(t, err)
	defer badResp.Body.Close()
	require.Equal(t, http.StatusBadRequest, badResp.StatusCode)
}

func TestPublishHTTPImagesAcceptsTheImageAlias(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)
	ts := newPublishTestServer(t, svc)

	resp, err := http.Post(ts.URL+"/api/v2/publish/images", "application/json",
		bytes.NewBufferString(`{"image":"example.com/hub/pingcap/widget/image:v8.5.0"}`))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusAccepted, resp.StatusCode)
}

func TestPublishHTTPNoMatchReturnsEmptySteps(t *testing.T) {
	svc := newPublishTestService(t, publishTestRules, &fakeImageOps{})
	ts := newPublishTestServer(t, svc)

	resp, err := http.Post(ts.URL+"/api/v2/publish/images", "application/json",
		bytes.NewBufferString(`{"artifactUrl":"example.com/hub/pingcap/unknown/image:v8.5.0"}`))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusAccepted, resp.StatusCode)

	var body struct {
		ID    string   `json:"id"`
		State string   `json:"state"`
		Steps []string `json:"steps"`
	}
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&body))
	require.Empty(t, body.ID)
	require.Equal(t, PublishStateSucceeded, body.State)
	require.Empty(t, body.Steps)
}
