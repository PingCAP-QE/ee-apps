package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

func TestUpdateComponentVersionInCloudconfig(t *testing.T) {
	const repo = "us.gcr.io/pingcap-public/tidbx/tidb"

	var (
		mu      sync.Mutex
		updates []map[string]any
	)
	ops := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "POST", r.Method)
		require.Equal(t, "secret-key", r.Header.Get("x-api-key"))

		var body map[string]any
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		body["_component"] = r.URL.Path

		mu.Lock()
		updates = append(updates, body)
		mu.Unlock()

		require.NoError(t, json.NewEncoder(w).Encode(map[string]any{"instance_id": 42}))
	}))
	t.Cleanup(ops.Close)

	tags := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/hotfix/tidbx/tag", r.URL.Path)
		require.Equal(t, "pingcap/tidb", r.URL.Query().Get("repo"))
		user, pass, ok := r.BasicAuth()
		require.True(t, ok)
		require.Equal(t, "tibuild-user", user)
		require.Equal(t, "tibuild-pass", pass)
		require.NoError(t, json.NewEncoder(w).Encode(map[string]any{
			"author": "alice@example.com",
			"meta": map[string]any{
				"ops_req": map[string]any{"release_id": "rel-1", "change_id": "chg-1"},
			},
		}))
	}))
	t.Cleanup(tags.Close)

	svc := newTidbcloudTestService(t, &OpsConfig{
		TiBuildV2: TiBuildV2Config{APIBaseURL: tags.URL, User: "tibuild-user", Password: "tibuild-pass"},
		Stages: map[string]OpsStageConfig{
			"prod": {
				APIBaseURL: ops.URL,
				APIKey:     "secret-key",
				Components: map[string]OpsComponent{
					"tidb_worker": {BaseImage: repo, ClusterType: "SHARED_POOL", GitHubRepo: "pingcap/tidb"},
					"tidb":        {BaseImage: repo, ClusterType: "SHARED_POOL", GitHubRepo: "pingcap/tidb"},
					"other":       {BaseImage: "example.com/other", ClusterType: "SHARED_POOL"},
				},
			},
		},
	}, nil)

	res, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: repo + ":v26.3.1-nextgen",
	})
	require.NoError(t, err)
	require.Len(t, res.Tickets, 2)

	// Components are sorted, so the request order is deterministic.
	require.Equal(t, "tidb", res.Tickets[0].Component)
	require.Equal(t, "tidb_worker", res.Tickets[1].Component)
	require.Equal(t, "v26.3.99", res.Tickets[0].ComponentVersion)
	require.Equal(t, "https://ops.tidbcloud.com/operations/42", res.Tickets[0].URL)
	require.Equal(t, "42", res.Tickets[0].ID)

	require.Len(t, updates, 2)
	for i, update := range updates {
		require.Equal(t, "/devops/cloudconfig/versions/component/"+res.Tickets[i].Component, update["_component"])
		require.Equal(t, "v26.3.99", update["version"])
		require.Equal(t, repo, update["base_image"])
		require.Equal(t, "v26.3.1-nextgen", update["tag"])
		require.Equal(t, "immediate", update["policy"])
		require.Equal(t, "SHARED_POOL", update["cluster_type"])
		require.Equal(t, "alice@example.com", update["author"])
		require.Equal(t, "rel-1", update["release_id"])
		require.Equal(t, "chg-1", update["change_id"])
	}
}

func TestUpdateComponentVersionInCloudconfigSkipsNonNextgenTag(t *testing.T) {
	called := false
	ops := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(ops.Close)

	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"prod": {APIBaseURL: ops.URL, Components: map[string]OpsComponent{
				"tidb": {BaseImage: "example.com/tidb"},
			}},
		},
	}, nil)

	res, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: "example.com/tidb:master",
	})
	require.NoError(t, err)
	require.Equal(t, "prod", res.Stage)
	require.Empty(t, res.Tickets)
	require.False(t, called)
}

func TestUpdateComponentVersionInCloudconfigRequiresConfig(t *testing.T) {
	svc := newTidbcloudTestService(t, nil, nil)
	_, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: "example.com/tidb:v26.3.1",
	})
	require.ErrorContains(t, err, "ops config is not configured")
}

func TestUpdateComponentVersionInCloudconfigUnknownStage(t *testing.T) {
	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: "http://127.0.0.1:1"},
		},
	}, nil)

	_, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: "example.com/tidb:v26.3.1",
	})
	require.ErrorContains(t, err, `stage "prod" not found`)
}
