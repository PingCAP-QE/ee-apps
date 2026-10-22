package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/google/go-github/v93/github"
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
	for product := range productBuildCapabilities {
		repos[product] = "tidbcloud/" + product
	}
	s := &devbuildsrvc{productRepoMap: repos}
	caps, err := s.Capabilities(context.Background())
	require.NoError(t, err)
	for _, product := range caps.Products {
		if _, ok := productBuildCapabilities[product.ID]; ok {
			require.Equal(t, []string{"linux", LinuxAmd64, LinuxArm64}, product.Platforms)
		} else {
			require.Contains(t, product.Platforms, "darwin")
		}
	}
	for product := range productBuildCapabilities {
		for _, platform := range []string{"linux", LinuxAmd64, LinuxArm64} {
			require.NoError(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, Platform: platform, Edition: "community"}))
		}
		for _, platform := range []string{"darwin", "all", DarwinAmd64} {
			require.Error(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, Platform: platform}))
		}
		require.Error(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, PipelineEngine: new("jenkins")}))
		require.Error(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, Edition: "enterprise"}))
		require.Error(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, IsHotfix: new(true)}))
		require.Error(t, validateProductRequest(&devbuild.DevBuildSpec{Product: product, PluginGitRef: new("branch/main")}))
	}
	require.NoError(t, validateProductRequest(&devbuild.DevBuildSpec{Product: "tidb", Platform: "darwin", Edition: "enterprise", PipelineEngine: new("jenkins"), IsHotfix: new(true), PluginGitRef: new("branch/main")}))
}

func TestServerlessEventsPinRevisionAndKeepCompatibilityVersion(t *testing.T) {
	s := &devbuildsrvc{} // No GitHub client: constructing events must not resolve a moving ref again.
	for product := range productBuildCapabilities {
		for _, ref := range []string{"branch/feature/test", "tag/v1.2.3", "pull/42", "commit/" + serverlessTestSHA} {
			record := &ent.DevBuild{ID: 7, Product: product, GithubRepo: "my-fork/custom-name", Edition: "community", GitRef: ref, GitHash: serverlessTestSHA, Version: "v99.0.0", Platform: "linux"}
			events, err := s.newDevBuildCloudEvents(record, gitRefInfoFromRecord(record))
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
			events, err = s.newDevBuildCloudEvents(record, gitRefInfoFromRecord(record))
			require.NoError(t, err)
			require.Len(t, events, 1)
		}
	}
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
	client, err := github.NewClient(github.WithHTTPClient(server.Client()), github.WithURLs(new(server.URL+"/"), nil))
	require.NoError(t, err)
	info, err := getGitRefInfo(context.Background(), client, "owner/repo", "tag/v1.2.3")
	require.NoError(t, err)
	require.Equal(t, serverlessTestSHA, info.SHA)
}

func TestCompletionWaitsForAllRequestedPlatforms(t *testing.T) {
	run := func(platform string) tknv1.PipelineRun {
		os, arch, _ := strings.Cut(platform, "/")
		var pr tknv1.PipelineRun
		pr.Spec.Params = tknv1.Params{{Name: "os", Value: *tknv1.NewStructuredValues(os)}, {Name: "arch", Value: *tknv1.NewStructuredValues(arch)}}
		pr.Status.Conditions = append(pr.Status.Conditions, knative.Condition{Type: knative.ConditionSucceeded, Status: "True"})
		return pr
	}
	am, ar, dm, dr := run(LinuxAmd64), run(LinuxArm64), run(DarwinAmd64), run(DarwinArm64)
	for _, product := range []string{"tidb-cse", "pd", "tidb"} {
		for _, tc := range []struct {
			name, platform, want string
			runs                 []tknv1.PipelineRun
		}{
			{"one linux platform missing", "linux", "PROCESSING", []tknv1.PipelineRun{am}},
			{"duplicate cannot replace missing platform", "linux", "PROCESSING", []tknv1.PipelineRun{am, am}},
			{"linux complete without artifacts", "linux", "SUCCESS", []tknv1.PipelineRun{am, ar}},
			{"default platforms", "", "PROCESSING", []tknv1.PipelineRun{am}},
			{"single platform", LinuxAmd64, "SUCCESS", []tknv1.PipelineRun{am}},
			{"darwin incomplete", "darwin", "PROCESSING", []tknv1.PipelineRun{dm}},
			{"darwin complete", "darwin", "SUCCESS", []tknv1.PipelineRun{dm, dr}},
			{"explicit list", LinuxAmd64 + "," + DarwinArm64, "SUCCESS", []tknv1.PipelineRun{am, dr}},
			{"all incomplete", "all", "PROCESSING", []tknv1.PipelineRun{am, ar}},
			{"all complete", "all", "SUCCESS", []tknv1.PipelineRun{am, ar, dm, dr}},
		} {
			t.Run(product+"/"+tc.name, func(t *testing.T) {
				require.Equal(t, tc.want, determineBuildStatus(&ent.DevBuild{Product: product, Platform: tc.platform}, tc.runs))
			})
		}
	}
	// Artifact publication is optional and does not change Tekton's success status.
	for _, results := range [][]tknv1.PipelineRunResult{
		{{Name: "pushed-images", Value: *tknv1.NewStructuredValues("images:\n- repo: test/image\n  tag: main-0123456_linux_amd64")}},
		{{Name: "pushed-binaries", Value: *tknv1.NewStructuredValues("oci:\n  repo: test/package\n  tag: main-0123456_linux_amd64\nfiles: [binary.tar.gz]")}},
		{{Name: "pushed-binaries", Value: *tknv1.NewStructuredValues(`"{}"`)}},
	} {
		am.Status.Results = results
		require.Equal(t, "SUCCESS", determineBuildStatus(&ent.DevBuild{Platform: LinuxAmd64}, []tknv1.PipelineRun{am}))
	}
	am.Status.Conditions[0].Status = "False"
	require.Equal(t, "FAILURE", determineBuildStatus(&ent.DevBuild{Platform: "linux"}, []tknv1.PipelineRun{am}))
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
		case strings.Contains(r.URL.Path, "/commits/"):
			_, _ = w.Write([]byte(`{"sha":"` + serverlessTestSHA + `"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	gh, err := github.NewClient(github.WithHTTPClient(server.Client()), github.WithURLs(new(server.URL+"/"), nil))
	require.NoError(t, err)
	s := &devbuildsrvc{dbClient: db, ghClient: gh, productRepoMap: map[string]string{}}
	for product := range productBuildCapabilities {
		s.productRepoMap[product] = "tidbcloud/" + product
		for _, ref := range []string{"branch/main", "tag/v1.2.3", "pull/42", "commit/" + serverlessTestSHA, serverlessTestSHA} {
			p := &devbuild.CreatePayload{Request: &devbuild.DevBuildSpec{Product: product, GitRef: ref, Platform: "linux", Version: new("v99.0.0")}}
			record, refInfo, err := s.newBuildEntity(context.Background(), p)
			require.NoError(t, err)
			require.Equal(t, serverlessTestSHA, record.GitHash)
			require.Equal(t, record.GitHash, refInfo.SHA)
			require.Equal(t, "v99.0.0", record.Version)
			require.Equal(t, "tekton", record.PipelineEngine)
			require.Equal(t, "tidbcloud/"+product, record.GithubRepo)
		}
	}
	p := &devbuild.CreatePayload{Request: &devbuild.DevBuildSpec{Product: "tidb-cse", GitRef: "branch/missing", Platform: "linux"}}
	_, _, err = s.newBuildEntity(context.Background(), p)
	var bad *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &bad)
	require.Contains(t, bad.Message, `branch "missing" not found`)
	p.Request.GitRef = "branch/main"
	p.Request.GithubRepo = new("custom-owner/custom-name")
	record, _, err := s.newBuildEntity(context.Background(), p)
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
	require.NoError(t, s.updateBuildFromPipelineRuns(ctx, build, []tknv1.PipelineRun{pr}))
	updated, err := db.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.Equal(t, "PROCESSING", updated.Status)
	require.Len(t, updated.TektonStatus.Pipelines, 1)
	require.Len(t, updated.BuildReport.Images, 1)
	require.Equal(t, serverlessTestSHA, updated.BuildReport.GitHash)
	require.True(t, updated.PipelineEndAt.IsZero())
}

func TestReconcileDoesNotRequireVersionOrArtifacts(t *testing.T) {
	for _, product := range []string{"tidb-cse", "pd"} {
		for _, printedVersion := range []string{"", "v8.5.3-alpha.0"} {
			t.Run(product+"/"+printedVersion, func(t *testing.T) {
				db := serverlessTestDB(t)
				ctx := context.Background()
				build, err := db.DevBuild.Create().SetProduct(product).SetPlatform(LinuxAmd64).
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
				if printedVersion != "" {
					pr.Status.Results = []tknv1.PipelineRunResult{{Name: "printed-version", Value: *tknv1.NewStructuredValues(printedVersion)}}
				}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					require.Equal(t, "/apis/tekton.dev/v1/pipelineruns/", r.URL.Path)
					w.Header().Set("Content-Type", "application/json")
					_ = json.NewEncoder(w).Encode(tknv1.PipelineRunList{Items: []tknv1.PipelineRun{pr}})
				}))
				defer server.Close()
				logger := zerolog.Nop()
				s := &devbuildsrvc{dbClient: db, logger: &logger, dashboardAPIURL: server.URL, dashboardViewURL: server.URL,
					httpClient: server.Client(), reconcilerSince: time.Hour}
				s.Reconcile(ctx)
				updated, err := db.DevBuild.Get(ctx, build.ID)
				require.NoError(t, err)
				require.Equal(t, "SUCCESS", updated.Status)
				require.Equal(t, "v99.0.0", updated.Version)
				require.Equal(t, printedVersion, updated.BuildReport.PrintedVersion)
				require.Equal(t, serverlessTestSHA, updated.BuildReport.GitHash)
				require.Empty(t, updated.BuildReport.Images)
				require.Empty(t, updated.BuildReport.Binaries)
			})
		}
	}
}
