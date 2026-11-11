package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// fakeMirror serves the two documents the reader needs: the signed snapshot
// (which names the current version of a component manifest) and that versioned
// component manifest.
type fakeMirror struct {
	server       *httptest.Server
	snapshotHits atomic.Int32
	manifestHits atomic.Int32

	component     string
	snapshotMeta  map[string]any
	manifestBody  string
	manifestCode  int
	snapshotCode  int
	snapshotBody  string
	manifestError bool
}

func newFakeMirror(t *testing.T, component string, version int, platforms map[string]any) *fakeMirror {
	t.Helper()
	m := &fakeMirror{
		component: component,
		snapshotMeta: map[string]any{
			"/" + component + ".json": map[string]any{"version": version, "length": 1234},
		},
	}
	m.manifestBody = mustJSON(t, map[string]any{
		"signed": map[string]any{
			"_type":     "component",
			"id":        component,
			"nightly":   "v9-nightly",
			"platforms": platforms,
		},
	})

	mux := http.NewServeMux()
	mux.HandleFunc("/snapshot.json", func(w http.ResponseWriter, r *http.Request) {
		m.snapshotHits.Add(1)
		if m.snapshotCode != 0 {
			w.WriteHeader(m.snapshotCode)
			return
		}
		body := m.snapshotBody
		if body == "" {
			body = mustJSON(t, map[string]any{"signed": map[string]any{"meta": m.snapshotMeta}})
		}
		_, _ = w.Write([]byte(body))
	})
	mux.HandleFunc(fmt.Sprintf("/%d.%s.json", version, component), func(w http.ResponseWriter, r *http.Request) {
		m.manifestHits.Add(1)
		if m.manifestCode != 0 {
			w.WriteHeader(m.manifestCode)
			return
		}
		if m.manifestError {
			_, _ = w.Write([]byte("{not json"))
			return
		}
		_, _ = w.Write([]byte(m.manifestBody))
	})
	m.server = httptest.NewServer(mux)
	t.Cleanup(m.server.Close)
	return m
}

func (m *fakeMirror) url() string { return m.server.URL }

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	require.NoError(t, err)
	return string(raw)
}

// mirrorPlatform builds one `platforms[<os>/<arch>][<version>]` entry.
func mirrorPlatform(sha256, url string, yanked bool) map[string]any {
	return map[string]any{
		"entry":  "pd",
		"hashes": map[string]any{"sha256": sha256},
		"length": 1024,
		"url":    url,
		"yanked": yanked,
	}
}

func TestMirrorReaderReadsPublishedSHA256(t *testing.T) {
	mirror := newFakeMirror(t, "pd", 7, map[string]any{
		"linux/amd64": map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/pd-nightly-linux-amd64.tar.gz", false)},
	})
	reader := newTiupMirrorReader(5 * time.Minute)

	got, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")

	require.NoError(t, err)
	require.True(t, got.Found)
	require.Equal(t, "aaaa", got.SHA256)
	require.Equal(t, "/pd-nightly-linux-amd64.tar.gz", got.URL)
}

func TestMirrorReaderReportsAbsent(t *testing.T) {
	t.Run("version missing", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, map[string]any{
			"linux/amd64": map[string]any{"v8-nightly": mirrorPlatform("aaaa", "/x.tar.gz", false)},
		})
		reader := newTiupMirrorReader(time.Minute)

		got, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")

		require.NoError(t, err, "a missing entry is a fact about the mirror, not a transport failure")
		require.False(t, got.Found)
	})

	t.Run("platform missing", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, map[string]any{
			"linux/arm64": map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/x.tar.gz", false)},
		})
		reader := newTiupMirrorReader(time.Minute)

		got, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")

		require.NoError(t, err)
		require.False(t, got.Found)
	})

	t.Run("version yanked", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, map[string]any{
			"linux/amd64": map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/x.tar.gz", true)},
		})
		reader := newTiupMirrorReader(time.Minute)

		got, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")

		require.NoError(t, err)
		require.False(t, got.Found, "a yanked entry is not a published artifact")
	})

	t.Run("component missing from the snapshot", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, nil)
		mirror.snapshotMeta = map[string]any{"/other.json": map[string]any{"version": 3}}
		reader := newTiupMirrorReader(time.Minute)

		got, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")

		require.NoError(t, err)
		require.False(t, got.Found)
	})
}

func TestMirrorReaderReportsErrors(t *testing.T) {
	t.Run("manifest unavailable", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, nil)
		mirror.manifestCode = http.StatusInternalServerError
		reader := newTiupMirrorReader(time.Minute)

		_, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")
		require.Error(t, err, "a broken mirror must not be read as \"not published\"")
	})

	t.Run("manifest not json", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, nil)
		mirror.manifestError = true
		reader := newTiupMirrorReader(time.Minute)

		_, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")
		require.Error(t, err)
	})

	t.Run("snapshot unavailable", func(t *testing.T) {
		mirror := newFakeMirror(t, "pd", 7, nil)
		mirror.snapshotCode = http.StatusBadGateway
		reader := newTiupMirrorReader(time.Minute)

		_, err := reader.publishedSHA256(context.Background(), mirror.url(), "pd", "linux", "amd64", "v9-nightly")
		require.Error(t, err)
	})
}

func TestMirrorReaderCachesComponent(t *testing.T) {
	mirror := newFakeMirror(t, "pd", 7, map[string]any{
		"linux/amd64":  map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/amd64.tar.gz", false)},
		"linux/arm64":  map[string]any{"v9-nightly": mirrorPlatform("bbbb", "/arm64.tar.gz", false)},
		"darwin/amd64": map[string]any{"v9-nightly": mirrorPlatform("cccc", "/d-amd64.tar.gz", false)},
		"darwin/arm64": map[string]any{"v9-nightly": mirrorPlatform("dddd", "/d-arm64.tar.gz", false)},
	})
	reader := newTiupMirrorReader(5 * time.Minute)
	ctx := context.Background()

	for _, p := range [][2]string{{"linux", "amd64"}, {"linux", "arm64"}, {"darwin", "amd64"}, {"darwin", "arm64"}} {
		got, err := reader.publishedSHA256(ctx, mirror.url(), "pd", p[0], p[1], "v9-nightly")
		require.NoError(t, err)
		require.True(t, got.Found)
	}

	require.Equal(t, int32(1), mirror.snapshotHits.Load(), "the snapshot is read once per cached component")
	require.Equal(t, int32(1), mirror.manifestHits.Load(), "four platforms must share one component manifest GET")
}

// nightlyRowWithFileDigest creates one desired row whose payload carries the
// tarball sha256 the verification compares against the mirror.
func nightlyRowWithFileDigest(t *testing.T, svc *publishsrvc, key desiredKey, version, fileDigest, lastSucceeded string, desiredAt time.Time) *ent.PublishDesired {
	t.Helper()
	payload := TiUPStepPayload{
		Mirror:         strings.TrimPrefix(key.Target, "tiup-"),
		MirrorURL:      "http://tiup.pingcap.net:8987",
		ArtifactURL:    "hub.pingcap.net/pingcap/pd/package:master_linux_amd64",
		Digest:         "sha256:manifest",
		FileDigest:     fileDigest,
		Nightly:        true,
		NightlyVersion: version,
		Packages: []delivery.TiUPPackage{{
			Name: key.Pkg, Version: version, OS: key.OS, Arch: key.Arch,
			EntryPoint: "pd-server", File: "pd-v9-nightly-linux-amd64.tar.gz",
		}},
	}
	raw, err := payloadMap(payload)
	require.NoError(t, err)

	create := svc.db.PublishDesired.Create().
		SetTarget(key.Target).
		SetPkg(key.Pkg).
		SetOs(key.OS).
		SetArch(key.Arch).
		SetDesiredVersion(version).
		SetDesiredPayload(raw).
		SetDesiredAt(desiredAt).
		SetNextRunAt(desiredAt.Add(time.Hour))
	if lastSucceeded != "" {
		create = create.SetLastSucceededVersion(lastSucceeded)
	}
	row, err := create.Save(context.Background())
	require.NoError(t, err)
	return row
}

// TestNightlyVerificationComparesFileDigest covers the decision itself: the
// mirror's tarball sha256 decides, not the (constant) version string.
func TestNightlyVerificationComparesFileDigest(t *testing.T) {
	mirror := newFakeMirror(t, "pd", 7, map[string]any{
		"linux/amd64": map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/pd-nightly-linux-amd64.tar.gz", false)},
	})
	svc, clock := newFlowTestService(t)
	svc.mirrorReader = newTiupMirrorReader(time.Minute)
	svc.publishCfg.TiUPMirrors = map[string]string{"prod": mirror.url(), "staging": mirror.url()}
	enabled := true
	svc.publishCfg.Nightly = nightlyTestConfig(enabled, delivery.TiUPTargetProd)
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }
	ctx := context.Background()

	key := desiredKeyForTest(delivery.TiUPTargetProd, "pd", "linux", "amd64")

	t.Run("matching sha256 is published", func(t *testing.T) {
		row := nightlyRowWithFileDigest(t, svc, key, "v9-nightly", "aaaa", "v9-nightly", clock.Add(-time.Hour))
		got := svc.verifyNightlyRow(ctx, row)
		require.True(t, got.Published)
		require.Equal(t, verifyResultPublished, got.Result)
		require.NoError(t, svc.db.PublishDesired.DeleteOneID(row.ID).Exec(ctx))
	})

	t.Run("different sha256 is unpublished even though the version matches", func(t *testing.T) {
		row := nightlyRowWithFileDigest(t, svc, key, "v9-nightly", "bbbb", "v9-nightly", clock.Add(-time.Hour))
		got := svc.verifyNightlyRow(ctx, row)
		require.False(t, got.Published, "the old version-string judgement would call this published")
		require.Equal(t, verifyResultMismatch, got.Result)
		require.Equal(t, "aaaa", got.MirrorSHA)
		require.NoError(t, svc.db.PublishDesired.DeleteOneID(row.ID).Exec(ctx))
	})

	t.Run("rows without a file digest fall back", func(t *testing.T) {
		row := nightlyRowWithFileDigest(t, svc, key, "v9-nightly", "", "v9-nightly", clock.Add(-time.Hour))
		got := svc.verifyNightlyRow(ctx, row)
		require.True(t, got.Published)
		require.Equal(t, verifyResultFallback, got.Result)
		require.NoError(t, svc.db.PublishDesired.DeleteOneID(row.ID).Exec(ctx))
	})
}

// TestNightlyAlertFiresWhenOnlyTheDigestDiffers is the regression this track
// exists for: a constant version string used to make every channel look
// published forever.
func TestNightlyAlertFiresWhenOnlyTheDigestDiffers(t *testing.T) {
	mirror := newFakeMirror(t, "pd", 7, map[string]any{
		"linux/amd64": map[string]any{"v9-nightly": mirrorPlatform("aaaa", "/pd-nightly-linux-amd64.tar.gz", false)},
	})
	svc, clock := newFlowTestService(t)
	svc.metrics = newNightlyMetrics()
	svc.nightlyAlerts = newNightlyAlertTracker(time.Hour)
	notifier := &recordingAlertNotifier{}
	svc.alerter = notifier
	svc.mirrorReader = newTiupMirrorReader(time.Minute)
	svc.publishCfg.TiUPMirrors = map[string]string{"prod": mirror.url(), "staging": mirror.url()}
	enabled := true
	svc.publishCfg.Nightly = nightlyTestConfig(enabled, delivery.TiUPTargetProd)
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }

	key := desiredKeyForTest(delivery.TiUPTargetProd, "pd", "linux", "amd64")
	// Published version equals the desired version: the old predicate says
	// "already published"; the digest proves otherwise.
	nightlyRowWithFileDigest(t, svc, key, "v9-nightly", "bbbb", "v9-nightly", clock.Add(-19*time.Hour))

	svc.refreshNightlyMetricsAndAlerts(context.Background())

	require.Equal(t, 1, notifier.count(), "a stale digest on a matching version must alert")
	body := notifier.lastBody()
	require.Contains(t, body, "sha256=bbbb", "the alert names the wanted tarball sha256")
	require.Contains(t, body, "mirror=aaaa", "and what the mirror actually holds")
	require.Contains(t, body, "next_run_at=")
}

// TestNightlyVerifyErrorKeepsTheOldJudgement makes sure a broken mirror cannot
// turn into a false alert.
func TestNightlyVerifyErrorKeepsTheOldJudgement(t *testing.T) {
	mirror := newFakeMirror(t, "pd", 7, nil)
	mirror.manifestCode = http.StatusInternalServerError
	svc, clock := newFlowTestService(t)
	svc.metrics = newNightlyMetrics()
	svc.nightlyAlerts = newNightlyAlertTracker(time.Hour)
	notifier := &recordingAlertNotifier{}
	svc.alerter = notifier
	svc.mirrorReader = newTiupMirrorReader(time.Minute)
	svc.publishCfg.TiUPMirrors = map[string]string{"prod": mirror.url(), "staging": mirror.url()}
	enabled := true
	svc.publishCfg.Nightly = nightlyTestConfig(enabled, delivery.TiUPTargetProd)
	svc.workerCfg.Targets = []string{delivery.TiUPTargetProd}
	svc.desired = newDesiredStore(svc.db, svc.publishCfg.Nightly)
	svc.desired.nowFn = func() time.Time { return *clock }

	key := desiredKeyForTest(delivery.TiUPTargetProd, "pd", "linux", "amd64")
	nightlyRowWithFileDigest(t, svc, key, "v9-nightly", "bbbb", "v9-nightly", clock.Add(-19*time.Hour))

	svc.refreshNightlyMetricsAndAlerts(context.Background())

	require.Equal(t, 0, notifier.count(), "an unreadable mirror keeps the previous judgement")

	body := scrapeNightlyMetrics(t, svc)
	require.Contains(t, body, `publish_desired_verify_total{mirror="prod",result="error"} 1`)
}

// recordingAlertNotifier keeps the bodies, so the alert text can be asserted.
type recordingAlertNotifier struct {
	fakeAlertNotifier
	bodies []string
}

func (r *recordingAlertNotifier) SendAlert(ctx context.Context, title, body string) error {
	r.bodies = append(r.bodies, body)
	return r.fakeAlertNotifier.SendAlert(ctx, title, body)
}

func (r *recordingAlertNotifier) lastBody() string {
	if len(r.bodies) == 0 {
		return ""
	}
	return r.bodies[len(r.bodies)-1]
}

func TestNightlyLagBodyNamesTheCause(t *testing.T) {
	row := &ent.PublishDesired{
		Target:         delivery.TiUPTargetProd,
		Pkg:            "cdc",
		Os:             "darwin",
		Arch:           "arm64",
		DesiredVersion: "v8.5.4-nextgen.202510.5-nightly",
		NextRunAt:      time.Date(2026, 11, 11, 14, 9, 59, 0, time.UTC),
		LastError:      "tiup mirror publish failed: exit status 1: The server refused",
	}
	results := []nightlyVerifyResult{
		{Row: row, Published: false, Result: verifyResultAbsent},
		{Row: row, Published: false, Result: verifyResultMismatch, MirrorSHA: "0123456789abcdef"},
		{Row: row, Published: false, Result: verifyResultFallback},
	}

	absent := nightlyLagBody(results[0], 58*time.Hour, 12*time.Hour)
	require.Contains(t, absent, "mirror=absent")
	require.Contains(t, absent, "next_run_at=2026-11-11T14:09:59Z")
	require.Contains(t, absent, "last_error=tiup mirror publish failed")

	mismatch := nightlyLagBody(results[1], 58*time.Hour, 12*time.Hour)
	require.Contains(t, mismatch, "mirror=0123456789ab")

	// A fallback row has no digest to show: name the judgement, not a fake hash.
	fallback := nightlyLagBody(results[2], 58*time.Hour, 12*time.Hour)
	require.Contains(t, fallback, "mirror=fallback")
	require.Contains(t, fallback, "sha256=unknown")
}

func TestMirrorReaderDefaultsItsCacheTTL(t *testing.T) {
	require.Equal(t, nightlyVerifyCacheTTL, newTiupMirrorReader(0).ttl)
}
