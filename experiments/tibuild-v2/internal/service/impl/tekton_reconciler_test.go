package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
	knative "knative.dev/pkg/apis"
)

func TestQueryPipelineRunsRequiresAPIURL(t *testing.T) {
	s := &devbuildsrvc{}
	_, err := s.queryPipelineRuns(context.Background(), "test-event")
	require.ErrorContains(t, err, "tekton api url is not configured")
}

func TestReconcileUsesSeparateAPIAndViewURLs(t *testing.T) {
	db := serverlessTestDB(t)
	ctx := context.Background()

	build, err := db.DevBuild.Create().
		SetProduct("tidb").
		SetPlatform(LinuxAmd64).
		SetVersion("v99.0.0").
		SetGitHash(serverlessTestSHA).
		SetStatus("PROCESSING").
		SetTektonStatus(schema.TektonStatus{TriggersEventIds: []string{"test-event"}}).
		Save(ctx)
	require.NoError(t, err)

	var apiRequests, viewRequests int
	apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiRequests++
		require.Equal(t, "/apis/tekton.dev/v1/pipelineruns/", r.URL.Path)

		var pr tknv1.PipelineRun
		pr.Name = "test-pipeline"
		pr.Namespace = "ee-cd"
		pr.Spec.Params = tknv1.Params{
			{Name: "os", Value: *tknv1.NewStructuredValues("linux")},
			{Name: "arch", Value: *tknv1.NewStructuredValues("amd64")},
		}
		pr.Status.Conditions = append(pr.Status.Conditions, knative.Condition{Type: knative.ConditionSucceeded, Status: "True"})

		w.Header().Set("Content-Type", "application/json")
		require.NoError(t, json.NewEncoder(w).Encode(tknv1.PipelineRunList{Items: []tknv1.PipelineRun{pr}}))
	}))
	defer apiServer.Close()

	viewServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		viewRequests++
		http.NotFound(w, r)
	}))
	defer viewServer.Close()

	logger := zerolog.Nop()
	s := &devbuildsrvc{
		dbClient:         db,
		logger:           &logger,
		dashboardAPIURL:  apiServer.URL,
		dashboardViewURL: viewServer.URL,
		httpClient:       apiServer.Client(),
		reconcilerSince:  time.Hour,
	}
	s.Reconcile(ctx)

	require.Equal(t, 1, apiRequests, "the reconciler must query the API URL once")
	require.Equal(t, 0, viewRequests, "the reconciler must never query the view URL")

	updated, err := db.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.Len(t, updated.TektonStatus.Pipelines, 1)
	require.Equal(t,
		viewServer.URL+"/#/namespaces/ee-cd/pipelineruns/test-pipeline",
		updated.TektonStatus.Pipelines[0].URL)
}
