package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/google/go-github/v69/github"
	_ "github.com/mattn/go-sqlite3"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
	knative "knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

const serverlessTestSHA = "0123456789abcdef0123456789abcdef01234567"

func TestServerlessCapabilitiesAndValidation(t *testing.T) {
	repos := map[string]string{"tidb": "pingcap/tidb"}
	for product := range serverlessProductLabels {
		repos[product] = "tidbcloud/" + product
	}
	s := &devbuildsrvc{productRepoMap: repos}
	caps, err := s.Capabilities(context.Background())
	require.NoError(t, err)
	for _, product := range caps.Products {
		if isServerlessProduct(product.ID) {
			require.Equal(t, []string{"linux", LinuxAmd64, LinuxArm64}, product.Platforms)
		} else {
			require.Contains(t, product.Platforms, "darwin")
		}
	}
	for product := range serverlessProductLabels {
		for _, platform := range []string{"linux", LinuxAmd64, LinuxArm64} {
			require.NoError(t, validateServerlessRequest(&devbuild.DevBuildSpec{Product: product, Platform: platform, Edition: "community"}))
		}
		for _, platform := range []string{"darwin", "all", DarwinAmd64} {
			require.Error(t, validateServerlessRequest(&devbuild.DevBuildSpec{Product: product, Platform: platform}))
		}
		require.Error(t, validateServerlessRequest(&devbuild.DevBuildSpec{Product: product, PipelineEngine: new("jenkins")}))
	}
}

func TestServerlessEventsPinRevisionAndKeepCompatibilityVersion(t *testing.T) {
	s := &devbuildsrvc{} // No GitHub client: constructing events must not resolve a moving ref again.
	for product := range serverlessProductLabels {
		for _, ref := range []string{"branch/feature/test", "tag/v1.2.3", "pull/42", "commit/" + serverlessTestSHA} {
			record := &ent.DevBuild{ID: 7, Product: product, GithubRepo: "my-fork/custom-name", Edition: "community", GitRef: ref, GitHash: serverlessTestSHA, Version: "v99.0.0", Platform: "linux"}
			events, err := s.newDevBuildCloudEvents(record)
			require.NoError(t, err)
			require.Len(t, events, 2)
			for _, event := range events {
				require.Equal(t, product, event.Extensions()["paramproduct"])
				require.Equal(t, serverlessTestSHA, event.Extensions()["paramgitrevision"])
				require.Equal(t, "v99.0.0", event.Extensions()["paramversion"])
				require.Equal(t, "community", event.Extensions()["paramprofile"])
				require.Equal(t, "7", event.Subject())
				if strings.HasPrefix(ref, "branch/") || strings.HasPrefix(ref, "commit/") {
					var payload github.PushEvent
					require.NoError(t, event.DataAs(&payload))
					require.Equal(t, serverlessTestSHA, payload.GetAfter())
				}
			}
			record.Platform = LinuxArm64
			events, err = s.newDevBuildCloudEvents(record)
			require.NoError(t, err)
			require.Len(t, events, 1)
		}
	}
	_, err := s.newDevBuildCloudEvent(&ent.DevBuild{Product: "tidb-cse", GitRef: "branch/main"}, LinuxAmd64)
	require.ErrorContains(t, err, "resolved commit SHA")
}

func TestServerlessAnnotatedTagResolvesCommit(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.Contains(r.URL.Path, "/git/ref/") {
			_ = json.NewEncoder(w).Encode(github.Reference{Object: &github.GitObject{Type: new("tag"), SHA: new("tag-object")}})
		} else if strings.Contains(r.URL.Path, "/git/tags/") {
			_ = json.NewEncoder(w).Encode(github.Tag{Object: &github.GitObject{Type: new("commit"), SHA: new(serverlessTestSHA)}})
		} else {
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := github.NewClient(server.Client())
	client.BaseURL, _ = url.Parse(server.URL + "/")
	_, sha := getGhRefAndSha(context.Background(), client, "owner/repo", "tag/v1.2.3")
	require.Equal(t, serverlessTestSHA, sha)
}

func TestServerlessCompletionWaitsForAllRequestedPlatforms(t *testing.T) {
	run := func(arch string) tknv1.PipelineRun {
		var pr tknv1.PipelineRun
		pr.Spec.Params = tknv1.Params{{Name: "os", Value: *tknv1.NewStructuredValues("linux")}, {Name: "arch", Value: *tknv1.NewStructuredValues(arch)}}
		pr.Status.Conditions = append(pr.Status.Conditions, knative.Condition{Type: knative.ConditionSucceeded, Status: "True"})
		pr.Status.Results = []tknv1.PipelineRunResult{{Name: "pushed-images", Value: *tknv1.NewStructuredValues("images:\n- repo: test/image\n  tag: main-0123456_linux_" + arch)}}
		pr.Status.Results = append(pr.Status.Results, tknv1.PipelineRunResult{Name: "pushed-binaries", Value: *tknv1.NewStructuredValues("oci:\n  repo: test/package\n  tag: main-0123456_linux_" + arch + "\nfiles: [binary.tar.gz, binary.tar.gz.sha256]")})
		return pr
	}
	am := run("amd64")
	ar := run("arm64")
	build := &ent.DevBuild{Product: "tidb-cse", Platform: "linux"}
	require.Equal(t, "PROCESSING", determineBuildStatus(build, []tknv1.PipelineRun{am}))
	require.Equal(t, "SUCCESS", determineBuildStatus(build, []tknv1.PipelineRun{am, ar}))
	// Images can be built directly without separately published binaries.
	am.Status.Results = am.Status.Results[:1]
	ar.Status.Results = ar.Status.Results[:1]
	require.Equal(t, "SUCCESS", determineBuildStatus(build, []tknv1.PipelineRun{am, ar}))
	// The existing Task also represents skipped binaries as a quoted empty object.
	am.Status.Results = append(am.Status.Results, tknv1.PipelineRunResult{Name: "pushed-binaries", Value: *tknv1.NewStructuredValues(`"{}"`)})
	require.Equal(t, "SUCCESS", determineBuildStatus(build, []tknv1.PipelineRun{am, ar}))
	ar.Status.Results = nil
	require.Equal(t, "FAILURE", determineBuildStatus(build, []tknv1.PipelineRun{am, ar}))
	build.Platform = LinuxAmd64
	require.Equal(t, "SUCCESS", determineBuildStatus(build, []tknv1.PipelineRun{am}))
}

func serverlessTestDB(t *testing.T) *ent.Client {
	t.Helper()
	db, err := ent.Open("sqlite3", "file:"+t.Name()+"?mode=memory&cache=shared&_fk=1")
	require.NoError(t, err)
	require.NoError(t, db.Schema.Create(context.Background()))
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestServerlessCreateResolvesRefsAndRejectsUnresolvableRefs(t *testing.T) {
	db := serverlessTestDB(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.Contains(r.URL.Path, "missing") {
			http.NotFound(w, r)
			return
		}
		switch {
		case strings.Contains(r.URL.Path, "/branches/"):
			_, _ = w.Write([]byte(`{"commit":{"sha":"` + serverlessTestSHA + `"}}`))
		case strings.Contains(r.URL.Path, "/git/ref/"):
			_, _ = w.Write([]byte(`{"object":{"type":"commit","sha":"` + serverlessTestSHA + `"}}`))
		case strings.Contains(r.URL.Path, "/pulls/"):
			_, _ = w.Write([]byte(`{"base":{"ref":"main"},"head":{"sha":"` + serverlessTestSHA + `"}}`))
		case strings.Contains(r.URL.Path, "/git/commits/"):
			_, _ = w.Write([]byte(`{"sha":"` + serverlessTestSHA + `"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	gh := github.NewClient(server.Client())
	gh.BaseURL, _ = url.Parse(server.URL + "/")
	s := &devbuildsrvc{dbClient: db, ghClient: gh, productRepoMap: map[string]string{}}
	for product := range serverlessProductLabels {
		s.productRepoMap[product] = "tidbcloud/" + product
		for _, ref := range []string{"branch/main", "tag/v1.2.3", "pull/42", "commit/" + serverlessTestSHA, serverlessTestSHA} {
			p := &devbuild.CreatePayload{Request: &devbuild.DevBuildSpec{Product: product, GitRef: ref, Platform: "linux", Version: new("v99.0.0")}}
			record, err := s.newBuildEntity(context.Background(), p)
			require.NoError(t, err)
			require.Equal(t, serverlessTestSHA, record.GitHash)
			require.Equal(t, "v99.0.0", record.Version)
			require.Equal(t, "tekton", record.PipelineEngine)
			require.Equal(t, "tidbcloud/"+product, record.GithubRepo)
		}
	}
	p := &devbuild.CreatePayload{Request: &devbuild.DevBuildSpec{Product: "tidb-cse", GitRef: "branch/missing", Platform: "linux"}}
	_, err := s.newBuildEntity(context.Background(), p)
	var bad *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &bad)
	require.Contains(t, bad.Message, "cannot resolve")
	p.Request.GitRef = "branch/main"
	p.Request.GithubRepo = new("custom-owner/custom-name")
	record, err := s.newBuildEntity(context.Background(), p)
	require.NoError(t, err)
	require.Equal(t, "custom-owner/custom-name", record.GithubRepo)
	require.Equal(t, "tidb-cse", record.Product)
}

func TestServerlessProcessingStillRefreshesReports(t *testing.T) {
	db := serverlessTestDB(t)
	ctx := context.Background()
	logger := zerolog.Nop()
	s := &devbuildsrvc{dbClient: db, logger: &logger}
	build, err := db.DevBuild.Create().SetProduct("tidb-cse").SetPlatform("linux").SetGitHash(serverlessTestSHA).SetStatus("PROCESSING").Save(ctx)
	require.NoError(t, err)
	var pr tknv1.PipelineRun
	pr.Name = "first-platform"
	pr.Spec.Params = tknv1.Params{{Name: "os", Value: *tknv1.NewStructuredValues("linux")}, {Name: "arch", Value: *tknv1.NewStructuredValues("amd64")}, {Name: "git-revision", Value: *tknv1.NewStructuredValues(serverlessTestSHA)}}
	pr.Status.Conditions = append(pr.Status.Conditions, knative.Condition{Type: knative.ConditionSucceeded, Status: "True"})
	pr.Status.Results = []tknv1.PipelineRunResult{{Name: "pushed-images", Value: *tknv1.NewStructuredValues("images:\n- repo: registry/image\n  tag: main-0123456_linux_amd64")}}
	require.NoError(t, s.updateBuildFromPipelineRuns(ctx, build, []tknv1.PipelineRun{pr}, "v8.5.3-alpha.0"))
	updated, err := db.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.Equal(t, "PROCESSING", updated.Status)
	require.Len(t, updated.TektonStatus.Pipelines, 1)
	require.Len(t, updated.BuildReport.Images, 1)
	require.Equal(t, serverlessTestSHA, updated.BuildReport.GitHash)
	require.Equal(t, "v8.5.3-alpha.0", updated.BuildReport.PrintedVersion)
	require.True(t, updated.PipelineEndAt.IsZero())
}

func TestServerlessNotificationSeparatesRequestedVersion(t *testing.T) {
	info := buildNotificationInfo(&ent.DevBuild{Product: "tidb-cse", Version: "v99.0.0", GitHash: serverlessTestSHA, BuildReport: schema.BuildReport{PrintedVersion: "v8.5.3-pre"}})
	require.Equal(t, "v99.0.0", info.Version)
	require.Equal(t, "v8.5.3-pre", info.ActualVersion)
	require.Equal(t, serverlessTestSHA, info.GitSha)
}

func TestServerlessVersionUsesExistingTaskResults(t *testing.T) {
	for _, tc := range []struct {
		name      string
		status    int
		body      string
		want      string
		wantError bool
	}{
		{"computed", http.StatusOK, `{"items":[{"status":{"results":[{"name":"version","value":"v8.5.3-alpha.0"}]}},{"status":{"results":[{"name":"version","value":"v8.5.3-alpha.0"}]}}]}`, "v8.5.3-alpha.0", false},
		{"not ready", http.StatusOK, `{"items":[]}`, "", false},
		{"different platforms", http.StatusOK, `{"items":[{"status":{"results":[{"name":"version","value":"v8.5.3-alpha.0"}]}},{"status":{"results":[{"name":"version","value":"v8.5.4"}]}}]}`, "", true},
		{"query failure", http.StatusForbidden, `{}`, "", true},
		{"invalid response", http.StatusOK, `{`, "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, "/apis/tekton.dev/v1/taskruns/", r.URL.Path)
				require.Equal(t, "tekton.dev/pipelineTask=get-release-ver,tekton.dev/pipelineRun in (run-amd64,run-arm64)", r.URL.Query().Get("labelSelector"))
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			s := &devbuildsrvc{dashboardURL: server.URL, httpClient: server.Client()}
			runs := make([]tknv1.PipelineRun, 2)
			runs[0].Name, runs[1].Name = "run-amd64", "run-arm64"
			version, err := s.queryReleaseVersion(context.Background(), runs)
			if tc.wantError {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
			}
			require.Equal(t, tc.want, version)
		})
	}
}

func TestServerlessReconcileKeepsPipelineResultsUnchanged(t *testing.T) {
	db := serverlessTestDB(t)
	ctx := context.Background()
	build, err := db.DevBuild.Create().SetProduct("tidb-cse").SetPlatform(LinuxAmd64).
		SetVersion("v99.0.0").SetGitHash(serverlessTestSHA).SetStatus("PROCESSING").
		SetTektonStatus(schema.TektonStatus{TriggersEventIds: []string{"test-event"}}).Save(ctx)
	require.NoError(t, err)
	var pr tknv1.PipelineRun
	pr.Name = "test-pipeline"
	pr.Spec.Params = tknv1.Params{
		{Name: "os", Value: *tknv1.NewStructuredValues("linux")},
		{Name: "arch", Value: *tknv1.NewStructuredValues("amd64")},
		{Name: "git-revision", Value: *tknv1.NewStructuredValues(serverlessTestSHA)},
	}
	pr.Status.Conditions = append(pr.Status.Conditions, knative.Condition{Type: knative.ConditionSucceeded, Status: "True"})
	pr.Status.Results = []tknv1.PipelineRunResult{
		{Name: "pushed-images", Value: *tknv1.NewStructuredValues("images:\n- repo: test/image\n  tag: main-0123456_linux_amd64")},
		{Name: "pushed-binaries", Value: *tknv1.NewStructuredValues("oci:\n  repo: test/package\n  tag: main-0123456_linux_amd64\nfiles: [binary.tar.gz, binary.tar.gz.sha256]")},
	}
	var versionReady atomic.Bool
	const taskVersion = "v8.5.3-alpha.0"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/apis/tekton.dev/v1/pipelineruns/":
			_ = json.NewEncoder(w).Encode(tknv1.PipelineRunList{Items: []tknv1.PipelineRun{pr}})
		case "/apis/tekton.dev/v1/taskruns/":
			if !versionReady.Load() {
				_, _ = w.Write([]byte(`{"items":[]}`))
			} else {
				_, _ = w.Write([]byte(`{"items":[{"status":{"results":[{"name":"version","value":"` + taskVersion + `"}]}}]}`))
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	logger := zerolog.Nop()
	s := &devbuildsrvc{dbClient: db, logger: &logger, dashboardURL: server.URL,
		httpClient: server.Client(), reconcilerSince: time.Hour}
	s.Reconcile(ctx)
	updated, err := db.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.Equal(t, "PROCESSING", updated.Status)
	require.Len(t, updated.BuildReport.Images, 1)
	versionReady.Store(true)
	s.Reconcile(ctx)
	updated, err = db.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.Equal(t, "SUCCESS", updated.Status)
	require.Equal(t, "v99.0.0", updated.Version)
	require.Equal(t, taskVersion, updated.BuildReport.PrintedVersion)
	require.Equal(t, serverlessTestSHA, updated.BuildReport.GitHash)
	require.Len(t, updated.BuildReport.Images, 1)
	require.Len(t, updated.BuildReport.Binaries, 1)
}
