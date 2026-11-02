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

func TestAddTidbxImageTagInTcms(t *testing.T) {
	var (
		gotBody map[string]any
		gotAuth string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/tidbx-component-image-builds", r.URL.Path)
		require.Equal(t, "POST", r.Method)
		gotAuth = r.Header.Get("Authorization")
		require.NoError(t, json.NewDecoder(r.Body).Decode(&gotBody))
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(srv.Close)

	svc := newTidbcloudTestService(t, nil, &TestPlatformsConfig{
		TCMS: TCMSConfig{APIBaseURL: srv.URL, AuthToken: "tcms-token"},
	})

	ref := "refs/heads/main"
	p := &tidbcloud.AddTidbxImageTagInTcmsPayload{Image: "example.com/tidb:v26.3.1"}
	p.Github = &struct {
		FullRepo  string
		Ref       *string
		CommitSha string
	}{
		FullRepo:  "pingcap/tidb",
		Ref:       &ref,
		CommitSha: "031069dfc0c70e839d996c9e1cf3d34930fc662f",
	}

	res, err := svc.AddTidbxImageTagInTcms(context.Background(), p)
	require.NoError(t, err)
	require.Equal(t, "v26.3.1", *res.ImageTag)
	require.Equal(t, "pingcap/tidb", *res.Repo)
	require.Equal(t, "main", *res.Branch)
	require.Equal(t, "031069dfc0c70e839d996c9e1cf3d34930fc662f", *res.Sha)

	require.Equal(t, "Bearer tcms-token", gotAuth)
	require.Equal(t, "pingcap/tidb", gotBody["repo"])
	require.Equal(t, "main", gotBody["branch"])
	require.Equal(t, "031069dfc0c70e839d996c9e1cf3d34930fc662f", gotBody["sha"])
	require.Equal(t, "v26.3.1", gotBody["imageTag"])
	require.NotContains(t, gotBody, "github")
}

func TestAddTidbxImageTagInTcmsRequiresConfig(t *testing.T) {
	svc := newTidbcloudTestService(t, nil, nil)
	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/tidb:v26.3.1",
	})
	require.ErrorContains(t, err, "test platforms config is not configured")
}

func TestAddTidbxImageTagInTcmsUpstreamError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusBadGateway)
	}))
	t.Cleanup(srv.Close)

	svc := newTidbcloudTestService(t, nil, &TestPlatformsConfig{
		TCMS: TCMSConfig{APIBaseURL: srv.URL},
	})
	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/tidb:v26.3.1",
	})
	require.ErrorContains(t, err, "call tcms api failed")
}

func TestAddTidbxImageTagInTcmsWithoutRef(t *testing.T) {
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, json.NewDecoder(r.Body).Decode(&gotBody))
		_, _ = w.Write([]byte(`{}`))
	}))
	t.Cleanup(srv.Close)

	svc := newTidbcloudTestService(t, nil, &TestPlatformsConfig{
		TCMS: TCMSConfig{APIBaseURL: srv.URL},
	})
	p := &tidbcloud.AddTidbxImageTagInTcmsPayload{Image: "example.com/tidb:v26.3.1"}
	p.Github = &struct {
		FullRepo  string
		Ref       *string
		CommitSha string
	}{
		FullRepo:  "pingcap/tidb",
		CommitSha: "031069dfc0c70e839d996c9e1cf3d34930fc662f",
	}

	res, err := svc.AddTidbxImageTagInTcms(context.Background(), p)
	require.NoError(t, err)
	require.Nil(t, res.Branch)
	require.NotContains(t, gotBody, "branch")
}
