package impl

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

var tiupTestPackage = delivery.TiUPPackage{
	Name:        "tidb",
	Version:     "v8.5.6",
	OS:          "linux",
	Arch:        "amd64",
	Description: "TiDB",
	EntryPoint:  "tidb-server",
	File:        "tidb-v8.5.6-linux-amd64.tar.gz",
}

// Only the released vX.Y.Z form is immutable. Every other form reuses its
// version string on purpose, so a new build must be allowed to replace it.
func TestIsFormalTiUPVersion(t *testing.T) {
	cases := []struct {
		version string
		formal  bool
	}{
		{version: "v8.5.6", formal: true},
		{version: "v26.3.1", formal: true},
		{version: "v8.5.6-release.1", formal: false},
		{version: "v8.5.6-beta.1", formal: false},
		{version: "v8.5.6-rc.1", formal: false},
		{version: "v8.5.9-pre", formal: false},
		{version: "v9.0.0-beta.2.pre-nightly", formal: false},
		{version: "v9.0.0-beta.2.pre.rust", formal: false},
		{version: "v26.3.1-nextgen", formal: false},
		{version: "release-8.5", formal: false},
		{version: "8.5.6", formal: false},
		{version: "v8.5", formal: false},
		{version: "v8.5.6.1", formal: false},
		{version: "v8.5.6 ", formal: false},
		{version: "", formal: false},
	}
	for _, tc := range cases {
		t.Run(tc.version, func(t *testing.T) {
			require.Equal(t, tc.formal, isFormalTiUPVersion(tc.version))
		})
	}
}

// fileMirror is a minimal mirror: GET serves stored files, PUT stores them.
type fileMirror struct {
	mu    sync.Mutex
	files map[string][]byte
}

func (m *fileMirror) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(r.URL.Path, "/")
		m.mu.Lock()
		defer m.mu.Unlock()

		switch r.Method {
		case http.MethodPut:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			m.files[name] = body
			w.WriteHeader(http.StatusCreated)
		case http.MethodGet:
			content, ok := m.files[name]
			if !ok {
				http.NotFound(w, r)
				return
			}
			_, _ = w.Write(content)
		default:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		}
	})
}

// writeFakeTiup installs an executable script that stands in for `tiup`.

// writeTempFile writes content to a temp file and returns its path and sha256.

// createTiUPStep persists a queued unit with one pending step.

func firstPackageResult(t *testing.T, result map[string]any) map[string]any {
	t.Helper()
	packages, ok := result[tiupResultPackages].([]map[string]any)
	require.True(t, ok, "packages result is %T", result[tiupResultPackages])
	require.Len(t, packages, 1)
	return packages[0]
}

func TestExecuteTiUPStepSkipsWhenRemoteMatches(t *testing.T) {
	content := []byte("payload")
	mirror := &fileMirror{files: map[string][]byte{tiupTestPackage.File: content}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	marker := filepath.Join(t.TempDir(), "tiup-called")
	svc.tiup.bin = writeFakeTiup(t, fmt.Sprintf("#!/bin/sh\necho called >> %s\n", marker))

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Digest:      "sha256:abc",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	result, err := svc.executeTiUPStep(context.Background(), step, "worker-1")
	require.NoError(t, err)
	require.Equal(t, true, result["skipped_existing"])
	pkgResult := firstPackageResult(t, result)
	require.Equal(t, tiupActionSkippedExisting, pkgResult["action"])
	require.Equal(t, "tidb", pkgResult["name"])
	require.Equal(t, "v8.5.6", pkgResult["version"])
	require.Equal(t, "linux", pkgResult["os"])
	require.Equal(t, "amd64", pkgResult["arch"])

	_, err = os.Stat(marker)
	require.True(t, os.IsNotExist(err), "an identical remote package must not be republished")
}

func TestExecuteTiUPStepRefusesConflictingFormal(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{tiupTestPackage.File: []byte("other content")}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	marker := filepath.Join(t.TempDir(), "tiup-called")
	svc.tiup.bin = writeFakeTiup(t, fmt.Sprintf("#!/bin/sh\necho called >> %s\n", marker))

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	_, err := svc.executeTiUPStep(context.Background(), step, "worker-1")
	require.ErrorContains(t, err, "refusing to overwrite formal version v8.5.6")

	// The refusal carries the coordinates the executor logs on failure.
	var refused *tiupOverwriteRefusedError
	require.ErrorAs(t, err, &refused)
	require.Equal(t, "v8.5.6", refused.pkg.Version)
	require.NotEmpty(t, refused.remoteSHA)
	require.NotEmpty(t, refused.localSHA)
	require.NotEqual(t, refused.remoteSHA, refused.localSHA)

	_, statErr := os.Stat(marker)
	require.True(t, os.IsNotExist(statErr), "a conflicting formal version must not be overwritten")

	mirror.mu.Lock()
	defer mirror.mu.Unlock()
	require.Equal(t, []byte("other content"), mirror.files[tiupTestPackage.File])
}

// Non-formal versions (pre/beta/rc/nightly/nextgen/rust/branch builds) reuse
// their version string on purpose, so a new build must replace the previous
// package instead of failing the publish.
func TestExecuteTiUPStepOverwritesNonFormal(t *testing.T) {
	cases := []struct {
		name    string
		version string
	}{
		{name: "pre release build", version: "v8.5.9-pre"},
		{name: "rust sql node build", version: "v9.0.0-beta.2.pre.rust"},
		{name: "nextgen build", version: "v26.3.1-nextgen"},
		{name: "empty version", version: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pkg := tiupTestPackage
			pkg.Version = tc.version
			pkg.File = fmt.Sprintf("tidb-%s-linux-amd64.tar.gz", tc.version)

			mirror := &fileMirror{files: map[string][]byte{pkg.File: []byte("yesterday")}}
			server := httptest.NewServer(mirror.handler())
			defer server.Close()

			svc := newTiUPTestService(t, server.URL)
			svc.tiup.bin = writeFakeTiup(t, fmt.Sprintf(`#!/bin/sh
name="$3"; version="$4"; file="$5"; os=""; arch=""
for arg in "$@"; do
  case "$prev" in
    --os) os="$arg" ;;
    --arch) arch="$arg" ;;
  esac
  prev="$arg"
done
curl -sS -X PUT --data-binary @"$file" "%s/$name-$version-$os-$arch.tar.gz" > /dev/null
`, server.URL))

			_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
				Mirror:      delivery.TiUPMirrorStaging,
				MirrorURL:   server.URL,
				ArtifactURL: "example.com/hub/pingcap/tidb/package:master_linux_amd64",
				Packages:    []delivery.TiUPPackage{pkg},
			})

			result, err := svc.executeTiUPStep(context.Background(), step, "worker-1")
			require.NoError(t, err)
			require.Equal(t, false, result["skipped_existing"])
			require.Equal(t, tiupActionOverwrittenNonFormal, firstPackageResult(t, result)["action"])

			mirror.mu.Lock()
			defer mirror.mu.Unlock()
			require.Equal(t, []byte("payload"), mirror.files[pkg.File], "the non-formal package must be replaced")
		})
	}
}

func TestExecuteTiUPStepPublishesAndVerifies(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	marker := filepath.Join(t.TempDir(), "tiup-called")

	// The fake tiup uploads the local tarball, like `tiup mirror publish`.
	logPath := filepath.Join(t.TempDir(), "tiup.log")
	// `tiup mirror publish` names the stored tarball from the package
	// coordinates, not from the local file name.
	script := fmt.Sprintf(`#!/bin/sh
echo called >> %s
echo "args: $@" >> %s
name="$3"; version="$4"; file="$5"; os=""; arch=""
for arg in "$@"; do
  case "$prev" in
    --os) os="$arg" ;;
    --arch) arch="$arg" ;;
  esac
  prev="$arg"
done
remote="$name-$version-$os-$arch.tar.gz"
curl -sS -X PUT --data-binary @"$file" "%s/$remote" >> %s 2>&1
echo "curl-exit=$? remote=$remote" >> %s
`, marker, logPath, server.URL, logPath, logPath)
	svc.tiup.bin = writeFakeTiup(t, script)

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	result, err := svc.executeTiUPStep(context.Background(), step, "worker-1")
	if err != nil {
		debug, _ := os.ReadFile(logPath)
		mirror.mu.Lock()
		keys := make([]string, 0, len(mirror.files))
		for k := range mirror.files {
			keys = append(keys, k)
		}
		mirror.mu.Unlock()
		t.Fatalf("execute failed: %v; tiup log:\n%s; mirror keys=%v", err, debug, keys)
	}
	require.Equal(t, false, result["skipped_existing"])
	pkgResult := firstPackageResult(t, result)
	require.Equal(t, tiupActionPublished, pkgResult["action"])
	require.Equal(t, "tidb", pkgResult["name"])
	require.Equal(t, "v8.5.6", pkgResult["version"])

	_, err = os.Stat(marker)
	require.NoError(t, err, "the package must be published")

	mirror.mu.Lock()
	defer mirror.mu.Unlock()
	require.Equal(t, []byte("payload"), mirror.files[tiupTestPackage.File])
}

func TestExecuteTiUPStepKillsTiupAtHardTimeout(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	svc.workerCfg.HardTimeout = "200ms"
	svc.tiup.bin = writeFakeTiup(t, "#!/bin/sh\nsleep 5\n")

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	started := time.Now()
	_, err := svc.executeTiUPStep(context.Background(), step, "worker-1")
	elapsed := time.Since(started)

	require.Error(t, err)
	require.Less(t, elapsed, 4*time.Second, "the tiup process must be killed at the deadline")
}

func TestExecuteTiUPStepFailsFastWhenLaneIsBusy(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	marker := filepath.Join(t.TempDir(), "tiup-called")
	svc.tiup.bin = writeFakeTiup(t, fmt.Sprintf("#!/bin/sh\necho called >> %s\n", marker))

	otherUnit, _ := createTiUPStep(t, svc, delivery.TiUPTargetProd, TiUPStepPayload{
		Mirror:    delivery.TiUPMirrorProd,
		MirrorURL: server.URL,
	})
	ok, err := svc.acquireLane(context.Background(), otherUnit.ID, "worker-other")
	require.NoError(t, err)
	require.True(t, ok)

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	_, err = svc.executeTiUPStep(context.Background(), step, "worker-1")
	require.ErrorIs(t, err, ErrLaneBusy)

	_, statErr := os.Stat(marker)
	require.True(t, os.IsNotExist(statErr), "a busy lane must not start a publish")
}

// A busy lane requeues the step instead of failing it: the mirror is legitimately
// owned by another unit.
func TestExecuteTiUPOnceRequeuesWhenLaneIsBusy(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	svc.tiup.bin = writeFakeTiup(t, "#!/bin/sh\nexit 0\n")
	svc.metrics = newNightlyMetrics()

	holderUnit, holderStep := createTiUPStep(t, svc, delivery.TiUPTargetProd, TiUPStepPayload{
		Mirror:    delivery.TiUPMirrorProd,
		MirrorURL: server.URL,
	})
	// Park the holder's own step so the claim loop only sees the staging step.
	require.NoError(t, svc.db.PublishStep.UpdateOneID(holderStep.ID).
		SetState(PublishStepStateSucceeded).
		Exec(context.Background()))

	ok, err := svc.acquireLane(context.Background(), holderUnit.ID, "worker-other")
	require.NoError(t, err)
	require.True(t, ok)

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})
	// The step has been waiting for the lane for 90s; the age of the queue row
	// is older, but only the wait counts.
	require.NoError(t, svc.db.PublishStep.UpdateOneID(step.ID).
		SetCreatedAt(svc.now().Add(-2*time.Minute)).
		SetStartedAt(svc.now().Add(-90*time.Second)).
		Exec(context.Background()))

	claimed, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, step.ID, claimed.ID)
	svc.executeStep(context.Background(), claimed)

	stored := stepByID(t, svc, step.ID)
	require.Equal(t, PublishStepStatePending, stored.State, "the step must be requeued, not failed")
	require.NotNil(t, stored.AvailableAt)

	body := scrapeNightlyMetrics(t, svc)
	require.Contains(t, body, `publish_lane_busy_total{target="tiup-staging"} 1`)
	require.Contains(t, body, `publish_lane_wait_seconds{target="tiup-staging"} 90`)
}

// A successful execution completes the step and converges the unit.
func TestExecuteTiUPOnceCompletesUnit(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{tiupTestPackage.File: []byte("payload")}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	svc.tiup.bin = writeFakeTiup(t, "#!/bin/sh\nexit 0\n")

	unit, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	claimed, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, claimed)
	svc.executeStep(context.Background(), claimed)

	require.Equal(t, PublishStepStateSucceeded, stepByID(t, svc, step.ID).State)
	require.Equal(t, PublishStateSucceeded, unitByID(t, svc, unit.ID).State)
}

// The nightly channel reuses one version string, so a new build must replace
// the previous package: refusing to overwrite would break nightly publishing.
func TestExecuteTiUPStepOverwritesNightly(t *testing.T) {
	nightlyFile := "tidb-v9.0.0-nightly-linux-amd64.tar.gz"
	mirror := &fileMirror{files: map[string][]byte{nightlyFile: []byte("yesterday")}}
	server := httptest.NewServer(mirror.handler())
	defer server.Close()

	svc := newTiUPTestService(t, server.URL)
	marker := filepath.Join(t.TempDir(), "tiup-called")
	script := fmt.Sprintf(`#!/bin/sh
echo called >> %s
name="$3"; version="$4"; file="$5"; os=""; arch=""
for arg in "$@"; do
  case "$prev" in
    --os) os="$arg" ;;
    --arch) arch="$arg" ;;
  esac
  prev="$arg"
done
curl -sS -X PUT --data-binary @"$file" "%s/$name-$version-$os-$arch.tar.gz" > /dev/null
`, marker, server.URL)
	svc.tiup.bin = writeFakeTiup(t, script)

	_, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:master_linux_amd64",
		Nightly:     true,
		Packages:    []delivery.TiUPPackage{{Name: "tidb", Version: "v9.0.0-nightly", OS: "linux", Arch: "amd64", EntryPoint: "tidb-server", File: tiupTestPackage.File}},
	})

	claimed, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, claimed)
	svc.executeStep(context.Background(), claimed)

	require.Equal(t, PublishStepStateSucceeded, stepByID(t, svc, step.ID).State)

	mirror.mu.Lock()
	defer mirror.mu.Unlock()
	require.Equal(t, []byte("payload"), mirror.files[nightlyFile], "the nightly package must be replaced")

	// The nightly overwrite keeps its own action, distinct from non-formal ones.
	stored := stepByID(t, svc, step.ID)
	packages, ok := stored.Result[tiupResultPackages].([]any)
	require.True(t, ok, "step result packages are %T", stored.Result[tiupResultPackages])
	require.Len(t, packages, 1)
	first, ok := packages[0].(map[string]any)
	require.True(t, ok, "step result package is %T", packages[0])
	require.Equal(t, tiupActionOverwrittenNightly, first[tiupResultAction])
}
