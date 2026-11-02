package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

func TestRequestSyncKernelImage(t *testing.T) {
	var gotBody map[string]any
	ops := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/devops/kernel-images/build-callback", r.URL.Path)
		require.Equal(t, "dev-key", r.Header.Get("x-api-key"))
		require.NoError(t, json.NewDecoder(r.Body).Decode(&gotBody))
		_, _ = w.Write([]byte(`{"message":"accepted"}`))
	}))
	t.Cleanup(ops.Close)

	tags := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "v8.5.4", r.URL.Query().Get("tag"))
		require.NoError(t, json.NewEncoder(w).Encode(map[string]any{
			"author": "bob@example.com",
			"meta": map[string]any{
				"ops_req": map[string]any{"release_id": "rel-9", "change_id": "chg-9"},
			},
		}))
	}))
	t.Cleanup(tags.Close)

	svc := newTidbcloudTestService(t, &OpsConfig{
		TiBuildV2: TiBuildV2Config{APIBaseURL: tags.URL},
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: ops.URL, APIKey: "dev-key"},
		},
	}, nil)
	svc.kernelImageMetaReader = func(_ context.Context, image string) kernelImageMeta {
		return kernelImageMeta{
			Repo:      "pingcap/tidb",
			GitTag:    "v8.5.4",
			CommitSHA: "abcdef0123456789abcdef0123456789abcdef01",
		}
	}

	body, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage: "dev",
		Images: []string{
			"us.gcr.io/pingcap-public/tidbx/tidb:v8.5.4",
			"us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4",
		},
	})
	require.NoError(t, err)
	require.JSONEq(t, `{"message":"accepted"}`, body)

	require.Equal(t, "bob@example.com", gotBody["source_applicant"])
	require.Equal(t, "rel-9", gotBody["source_release_id"])
	require.Equal(t, "chg-9", gotBody["source_change_id"])
	require.Equal(t, "pingcap/tidb", gotBody["repo"])
	require.Equal(t, "v8.5.4", gotBody["git_tag"])
	require.Equal(t, "abcdef0123456789abcdef0123456789abcdef01", gotBody["commit_sha"])
	require.NotContains(t, gotBody, "branch")

	images := gotBody["images"].([]any)
	require.Len(t, images, 2)
	first := images[0].(map[string]any)
	require.Equal(t, "us.gcr.io/pingcap-public/tidbx/tidb", first["source_repository"])
	require.Equal(t, "v8.5.4", first["source_tag"])
}

func TestRequestSyncKernelImageRejectsMixedCommits(t *testing.T) {
	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: "http://127.0.0.1:1"},
		},
	}, nil)
	svc.kernelImageMetaReader = func(_ context.Context, image string) kernelImageMeta {
		sha := "aaaa000000000000000000000000000000000000"
		if image == "example.com/tikv:v8.5.4" {
			sha = "bbbb000000000000000000000000000000000000"
		}
		return kernelImageMeta{Repo: "pingcap/tidb", GitTag: "v8.5.4", CommitSHA: sha}
	}

	_, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage:  "dev",
		Images: []string{"example.com/tidb:v8.5.4", "example.com/tikv:v8.5.4"},
	})
	require.ErrorContains(t, err, "not built from the same repo commit")
}

func TestRequestSyncKernelImageSkipsMetadataForDailyTag(t *testing.T) {
	metadataCalls := 0
	tags := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		metadataCalls++
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(tags.Close)

	ops := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{}`))
	}))
	t.Cleanup(ops.Close)

	svc := newTidbcloudTestService(t, &OpsConfig{
		TiBuildV2: TiBuildV2Config{APIBaseURL: tags.URL},
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: ops.URL},
		},
	}, nil)
	svc.kernelImageMetaReader = func(_ context.Context, image string) kernelImageMeta {
		return kernelImageMeta{Repo: "pingcap/tidb", Branch: "main", CommitSHA: "abcdef0123456789abcdef0123456789abcdef01"}
	}

	_, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage:  "dev",
		Images: []string{"example.com/tidb:main-abcdef01"},
	})
	require.NoError(t, err)
	require.Zero(t, metadataCalls)
}

func TestRequestSyncKernelImageRequiresSameRepoLabels(t *testing.T) {
	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: "http://127.0.0.1:1"},
		},
	}, nil)
	svc.kernelImageMetaReader = func(context.Context, string) kernelImageMeta {
		return kernelImageMeta{}
	}

	_, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage:  "dev",
		Images: []string{"example.com/tidb:v8.5.4"},
	})
	require.ErrorContains(t, err, "missing repo or commit_sha labels")
}
