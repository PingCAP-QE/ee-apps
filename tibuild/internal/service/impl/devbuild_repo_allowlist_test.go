package impl

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-github/v93/github"
	_ "github.com/mattn/go-sqlite3"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

const allowlistTestSHA = "0123456789abcdef0123456789abcdef01234567"

// allowlistTestGitHub serves the read endpoints getGitRefInfo needs so the
// request path never touches the network.
func allowlistTestGitHub(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.Contains(r.URL.Path, "/branches/"):
			_, _ = w.Write([]byte(`{"commit":{"sha":"` + allowlistTestSHA + `"}}`))
		case strings.Contains(r.URL.Path, "/git/ref/"):
			_, _ = w.Write([]byte(`{"object":{"type":"commit","sha":"` + allowlistTestSHA + `"}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

type allowlistTestEnv struct {
	service *devbuildsrvc
	db      *ent.Client
	// events records how many CloudEvents reached the fake listener.
	events func() int
}

func newAllowlistTestEnv(t *testing.T, allow []string, productRepoMap map[string]string) *allowlistTestEnv {
	t.Helper()
	db := serverlessTestDB(t)
	ghServer := allowlistTestGitHub(t)
	ghClient, err := github.NewClient(github.WithHTTPClient(ghServer.Client()), github.WithURLs(new(ghServer.URL+"/"), nil))
	require.NoError(t, err)

	ceServer, _ := newCloudEventRecordingServer(t)
	var mu sync.Mutex
	eventCount := 0
	countingServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		eventCount++
		mu.Unlock()
		// Mirror the fake EventListener: 2xx on accepted events.
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(countingServer.Close)
	_ = ceServer

	logger := zerolog.Nop()
	ce, err := newTektonCloudEventClient(countingServer.URL, "", &logger)
	require.NoError(t, err)

	if productRepoMap == nil {
		productRepoMap = map[string]string{
			"tidb": "pingcap/tidb",
			"pd":   "pingcap/pd",
			"cse":  "tidbcloud/cloud-storage-engine",
		}
	}

	allowlist, err := config.NewRepoAllowlist(allow)
	require.NoError(t, err)

	s := &devbuildsrvc{
		logger:                 &logger,
		repoMetrics:            newDevbuildRepoMetrics(prometheus.NewRegistry()),
		dbClient:               db,
		productRepoMap:         productRepoMap,
		ghClient:               ghClient,
		tektonCloudEventClient: ce,
		httpClient:             &http.Client{Timeout: 5 * time.Second},
	}
	s.repoAllowlist.Store(&allowlist)
	return &allowlistTestEnv{
		service: s,
		db:      db,
		events: func() int {
			mu.Lock()
			defer mu.Unlock()
			return eventCount
		},
	}
}

func (e *allowlistTestEnv) count(t *testing.T) int {
	t.Helper()
	n, err := e.db.DevBuild.Query().Count(context.Background())
	require.NoError(t, err)
	return n
}

func TestCreateRepoAllowlist(t *testing.T) {
	repoOf := func(value string) *string { return &value }

	tests := []struct {
		name        string
		allow       []string
		product     string
		explicit    *string
		dryrun      bool
		wantRepo    string
		wantStatus  int
		wantMessage string
	}{
		{
			name:     "allowlisted repository is accepted",
			allow:    []string{"pingcap/tidb"},
			product:  "tidb",
			explicit: repoOf("pingcap/tidb"),
			wantRepo: "pingcap/tidb",
		},
		{
			name:     "matching is case-insensitive and keeps the caller value",
			allow:    []string{"pingcap/tidb"},
			product:  "tidb",
			explicit: repoOf("PingCAP/TiDB"),
			wantRepo: "PingCAP/TiDB",
		},
		{
			name:        "repository outside the list is forbidden",
			allow:       []string{"pingcap/tidb"},
			product:     "tidb",
			explicit:    repoOf("evil/fork"),
			wantStatus:  http.StatusForbidden,
			wantMessage: "githubRepo 'evil/fork' is not in the devbuild allowlist",
		},
		{
			name:     "dryrun applies the same rule",
			allow:    []string{"pingcap/tidb"},
			product:  "tidb",
			explicit: repoOf("pingcap/tidb"),
			dryrun:   true,
			wantRepo: "pingcap/tidb",
		},
		{
			name:        "dryrun denies the same way",
			allow:       []string{"pingcap/tidb"},
			product:     "tidb",
			explicit:    repoOf("evil/fork"),
			dryrun:      true,
			wantStatus:  http.StatusForbidden,
			wantMessage: "githubRepo 'evil/fork' is not in the devbuild allowlist",
		},
		{
			name:     "empty list accepts an explicit value equal to the product default",
			allow:    nil,
			product:  "pd",
			explicit: repoOf("pingcap/pd"),
			wantRepo: "pingcap/pd",
		},
		{
			name:        "empty list rejects any other explicit value",
			allow:       nil,
			product:     "pd",
			explicit:    repoOf("evil/fork"),
			wantStatus:  http.StatusForbidden,
			wantMessage: "githubRepo 'evil/fork' is not in the devbuild allowlist",
		},
		{
			name:     "missing githubRepo uses the product default and skips the lookup",
			allow:    nil,
			product:  "pd",
			wantRepo: "pingcap/pd",
		},
		{
			name:     "owner wildcard covers any repository of that owner",
			allow:    []string{"tidbcloud/*"},
			product:  "cse",
			explicit: repoOf("tidbcloud/cloud-storage-engine"),
			wantRepo: "tidbcloud/cloud-storage-engine",
		},
		{
			name:        "owner wildcard does not cover another owner",
			allow:       []string{"tidbcloud/*"},
			product:     "cse",
			explicit:    repoOf("other/cloud-storage-engine"),
			wantStatus:  http.StatusForbidden,
			wantMessage: "not in the devbuild allowlist",
		},
		{
			name:        "unknown product without an explicit repo is still a bad request",
			allow:       nil,
			product:     "nope",
			wantStatus:  http.StatusBadRequest,
			wantMessage: "unknown product or githubRepo",
		},
		{
			name:        "malformed repository is still a bad request",
			allow:       []string{"pingcap/tidb"},
			product:     "tidb",
			explicit:    repoOf("owner/repo/extra"),
			wantStatus:  http.StatusBadRequest,
			wantMessage: "githubRepo must be in the form <owner>/<repo>",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := newAllowlistTestEnv(t, tc.allow, nil)
			payload := &devbuild.CreatePayload{
				Request: &devbuild.DevBuildSpec{
					Product:    tc.product,
					GithubRepo: tc.explicit,
					GitRef:     "branch/main",
					Platform:   "linux/amd64",
				},
				CreatedBy: stringPtr("tester@example.com"),
				Dryrun:    tc.dryrun,
			}

			record, err := env.service.Create(context.Background(), payload)
			if tc.wantStatus == 0 {
				require.NoError(t, err)
				require.Equal(t, tc.wantRepo, derefString(record.Spec.GithubRepo))
				return
			}

			require.Error(t, err)
			switch tc.wantStatus {
			case http.StatusForbidden:
				var forbidden *devbuild.DevBuildForbiddenError
				require.ErrorAs(t, err, &forbidden)
				require.Equal(t, http.StatusForbidden, forbidden.Code)
				require.Contains(t, forbidden.Message, tc.wantMessage)
				// The message must not leak what is allowed.
				for _, entry := range tc.allow {
					require.NotContains(t, forbidden.Message, entry)
				}
			case http.StatusBadRequest:
				var badRequest *devbuild.DevBuildBadRequestError
				require.ErrorAs(t, err, &badRequest)
				require.Equal(t, http.StatusBadRequest, badRequest.Code)
				require.Contains(t, badRequest.Message, tc.wantMessage)
			}
			require.Equal(t, 0, env.count(t), "a rejected request must not store a build")
			require.Equal(t, 0, env.events(), "a rejected request must not send a CloudEvent")
		})
	}
}

func TestCreateDeniedDoesNotPersistOrTrigger(t *testing.T) {
	env := newAllowlistTestEnv(t, []string{"pingcap/tidb"}, nil)
	_, err := env.service.Create(context.Background(), &devbuild.CreatePayload{
		Request: &devbuild.DevBuildSpec{
			Product:    "tidb",
			GithubRepo: stringPtr("evil/fork"),
			GitRef:     "branch/main",
			Platform:   "linux/amd64",
		},
		CreatedBy: stringPtr("tester@example.com"),
	})
	require.Error(t, err)
	require.Equal(t, 0, env.count(t))
	require.Equal(t, 0, env.events())
}

func TestRerunRepoAllowlist(t *testing.T) {
	tests := []struct {
		name     string
		allow    []string
		record   string
		status   string
		wantErr  bool
		wantRepo string
	}{
		{
			name:    "repository removed from the list cannot be rerun",
			allow:   []string{"pingcap/pd"},
			record:  "evil/fork",
			status:  "SUCCESS",
			wantErr: true,
		},
		{
			name:     "repository still in the list reruns",
			allow:    []string{"pingcap/pd", "evil/fork"},
			record:   "evil/fork",
			status:   "SUCCESS",
			wantRepo: "evil/fork",
		},
		{
			name:     "legacy record without a repository falls back to the product default",
			allow:    []string{"pingcap/pd"},
			record:   "",
			status:   "SUCCESS",
			wantRepo: "pingcap/pd",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := newAllowlistTestEnv(t, tc.allow, nil)
			seed := env.db.DevBuild.Create().
				SetProduct("pd").
				SetEdition("").
				SetVersion("v1.0.0").
				SetGithubRepo(tc.record).
				SetGitRef("branch/main").
				SetGitHash(allowlistTestSHA).
				SetPlatform("linux/amd64").
				SetStatus(tc.status).
				SetCreatedBy("tester@example.com").
				SetCreatedAt(time.Now()).
				SaveX(context.Background())

			before := env.count(t)
			record, err := env.service.Rerun(context.Background(), &devbuild.RerunPayload{ID: seed.ID})
			if tc.wantErr {
				require.Error(t, err)
				var forbidden *devbuild.DevBuildForbiddenError
				require.ErrorAs(t, err, &forbidden)
				require.Equal(t, http.StatusForbidden, forbidden.Code)
				require.Equal(t, before, env.count(t), "a rejected rerun must not store a build")
				require.Equal(t, 0, env.events(), "a rejected rerun must not send a CloudEvent")
				return
			}
			require.NoError(t, err)
			require.Equal(t, tc.wantRepo, derefString(record.Spec.GithubRepo))
			require.Equal(t, before+1, env.count(t))
			require.Equal(t, 1, env.events())
		})
	}
}

// TestReloadPublishesRepoAllowlist covers the hot path: a new revision widens
// then narrows what requests may name, without recreating the service.
func TestReloadPublishesRepoAllowlist(t *testing.T) {
	env := newAllowlistTestEnv(t, nil, nil)
	ctx := context.Background()
	payload := func(repo string) *devbuild.CreatePayload {
		return &devbuild.CreatePayload{
			Request: &devbuild.DevBuildSpec{
				Product:    "tidb",
				GithubRepo: stringPtr(repo),
				GitRef:     "branch/main",
				Platform:   "linux/amd64",
			},
			CreatedBy: stringPtr("tester@example.com"),
			Dryrun:    true,
		}
	}

	_, err := env.service.Create(ctx, payload("tidbcloud/cloud-storage-engine"))
	require.Error(t, err, "the empty list must reject a non-default repository")

	// Reload rebuilds the GitHub client from the new config; keep the test
	// server so ref resolution stays offline.
	ghClient := env.service.ghClient
	env.service.Reload(&config.Service{
		ProductRepoMap: env.service.productRepoMap,
		DevBuild: config.DevBuild{
			GithubRepoAllowlist: []string{"tidbcloud/*"},
		},
	})
	env.service.ghClient = ghClient
	_, err = env.service.Create(ctx, payload("tidbcloud/cloud-storage-engine"))
	require.NoError(t, err, "the reloaded list must accept the repository")

	env.service.Reload(&config.Service{
		ProductRepoMap: env.service.productRepoMap,
		DevBuild:       config.DevBuild{GithubRepoAllowlist: []string{"pingcap/tidb"}},
	})
	env.service.ghClient = ghClient
	_, err = env.service.Create(ctx, payload("tidbcloud/cloud-storage-engine"))
	require.Error(t, err, "removing the entry must reject the repository again")
}

// TestRepoAllowlistConcurrentReloadAndRequests is the race-detector cover for
// the atomic publication: requests read the current list while the watcher
// replaces it.
func TestRepoAllowlistConcurrentReloadAndRequests(t *testing.T) {
	env := newAllowlistTestEnv(t, []string{"pingcap/tidb"}, nil)
	s := env.service
	repo := "tidbcloud/cloud-storage-engine"

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 40; j++ {
				entry := "tidbcloud/*"
				if (i+j)%2 == 0 {
					entry = "pingcap/tidb"
				}
				allow, err := config.NewRepoAllowlist([]string{entry})
				if err != nil {
					t.Errorf("NewRepoAllowlist() error = %v", err)
					return
				}
				s.repoAllowlist.Store(&allow)
				_ = s.currentAllowlist().Allows(repo)
				_ = s.currentAllowlist().Empty()
				_, _ = s.resolveRepoProduct(repo)
			}
		}(i)
	}
	wg.Wait()
}

// resolveRepoProduct is a small read-side probe so the goroutines exercise the
// same helper the request path uses.
func (s *devbuildsrvc) resolveRepoProduct(repo string) (string, bool) {
	return repo, s.currentAllowlist().Allows(repo)
}

// TestRepoDeniedCounter proves the rejection is observable: one increment per
// surface, and a nil registry keeps the service silent instead of panicking.
func TestRepoDeniedCounter(t *testing.T) {
	registry := prometheus.NewRegistry()
	metrics := newDevbuildRepoMetrics(registry)
	require.NotNil(t, metrics)

	logger := zerolog.Nop()
	s := &devbuildsrvc{logger: &logger, repoMetrics: metrics}
	s.recordRepoDenied("create", "evil/fork", "tidb", "tester@example.com")
	s.recordRepoDenied("create", "evil/other", "tidb", "tester@example.com")
	s.recordRepoDenied("rerun", "evil/fork", "tidb", "tester@example.com")

	families, err := registry.Gather()
	require.NoError(t, err)
	require.Len(t, families, 1)
	require.Equal(t, "devbuild_github_repo_denied_total", families[0].GetName())

	got := map[string]float64{}
	for _, metric := range families[0].GetMetric() {
		got[metric.GetLabel()[0].GetValue()] = metric.GetCounter().GetValue()
	}
	require.Equal(t, map[string]float64{"create": 2, "rerun": 1}, got)

	// No registry (metrics endpoint disabled) must be a no-op, not a panic.
	s.repoMetrics = newDevbuildRepoMetrics(nil)
	s.recordRepoDenied("create", "evil/fork", "tidb", "tester@example.com")

	// A logger-less service still counts, which keeps the metric usable in
	// tests that build a bare service.
	bare := &devbuildsrvc{repoMetrics: newDevbuildRepoMetrics(prometheus.NewRegistry())}
	bare.recordRepoDenied("rerun", "evil/fork", "tidb", "")
}
