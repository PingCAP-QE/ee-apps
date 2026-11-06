package impl

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	entpublishunit "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishunit"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

type supersedeArtifact struct {
	config  map[string]any
	digest  string
	created time.Time
}

func newSupersedeService(t *testing.T, artifacts map[string]*supersedeArtifact) (*publishsrvc, *time.Time) {
	t.Helper()
	svc, clock := newFlowTestService(t)
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}
	svc.fetchConfig = func(ctx context.Context, artifactURL string) (map[string]any, string, time.Time, error) {
		artifact, ok := artifacts[artifactURL]
		if !ok {
			return nil, "", time.Time{}, errors.New("unexpected artifact " + artifactURL)
		}
		return artifact.config, artifact.digest, artifact.created, nil
	}
	return svc, clock
}

// The same channel with a different non-formal digest keeps only the newest
// unit; the older unit's steps are skipped with an audit trail.
func TestSupersedeNonFormalKeepsNewest(t *testing.T) {
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[nightlyArtifact].ArtifactConfig,
		digest:  "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()

	first, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, first, 2)

	*clock = clock.Add(time.Minute)
	artifact.digest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	artifact.created = *clock
	second, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, second, 2)
	require.NotEqual(t, first[0], second[0])

	units, err := svc.db.PublishUnit.Query().
		Order(ent.Asc(entpublishunit.FieldCreatedAt)).
		All(ctx)
	require.NoError(t, err)
	require.Len(t, units, 2)
	require.Equal(t, PublishStateCanceled, units[0].State)
	require.Equal(t, PublishStateQueued, units[1].State)

	oldSteps, err := svc.db.PublishStep.Query().
		Where(entpublishstep.IDIn(first...)).
		All(ctx)
	require.NoError(t, err)
	require.Len(t, oldSteps, 2)
	for _, step := range oldSteps {
		require.Equal(t, PublishStepStateSkipped, step.State)
		require.Contains(t, step.LastError, "superseded by "+units[1].ID)
	}
}

// Re-submitting the same digest is idempotent: no new unit, same step ids.
func TestSupersedeSameDigestIsIdempotent(t *testing.T) {
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[nightlyArtifact].ArtifactConfig,
		digest:  "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()

	first, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	*clock = clock.Add(time.Minute)
	second, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)

	require.ElementsMatch(t, first, second)
	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)
}

// A formal vX.Y.Z with a different digest is rejected at enqueue with 409.
func TestFormalVersionConflictRejected(t *testing.T) {
	const artifactURL = "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_linux_amd64"
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[artifactURL].ArtifactConfig,
		digest:  "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{artifactURL: artifact})
	ctx := context.Background()
	version := "v8.5.6"

	ids, err := svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: artifactURL,
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	require.NoError(t, err)
	require.Len(t, ids, 1)

	*clock = clock.Add(time.Minute)
	artifact.digest = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
	artifact.created = *clock

	_, err = svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: artifactURL,
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	var conflict *publish.PublishConflictError
	require.ErrorAs(t, err, &conflict)
	require.Equal(t, http.StatusConflict, conflict.Code)
	require.Contains(t, conflict.Message, "formal version v8.5.6")
	require.NotNil(t, conflict.ExistingDigest)
	require.Equal(t, artifact.digest, *conflict.NewDigest)

	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count, "the conflicting request must not create a unit")
}

// A unit that already has a claimed step is not cancelled; the new unit still
// gets queued and will publish after it.
func TestSupersedeSkipsClaimedUnit(t *testing.T) {
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[nightlyArtifact].ArtifactConfig,
		digest:  "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()

	first, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.NoError(t, svc.db.PublishStep.UpdateOneID(first[0]).
		SetState(PublishStepStateClaimed).
		Exec(ctx))

	*clock = clock.Add(time.Minute)
	artifact.digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	artifact.created = *clock
	_, err = svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)

	units, err := svc.db.PublishUnit.Query().
		Order(ent.Asc(entpublishunit.FieldCreatedAt)).
		All(ctx)
	require.NoError(t, err)
	require.Len(t, units, 2)
	require.NotEqual(t, PublishStateCanceled, units[0].State)
}

// A formal vX.Y.Z with the same digest is idempotent, not a conflict.
func TestFormalVersionSameDigestIdempotent(t *testing.T) {
	const artifactURL = "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_linux_amd64"
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[artifactURL].ArtifactConfig,
		digest:  "sha256:7777777777777777777777777777777777777777777777777777777777777777",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{artifactURL: artifact})
	ctx := context.Background()
	version := "v8.5.6"

	first, err := svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: artifactURL,
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	require.NoError(t, err)
	*clock = clock.Add(time.Minute)
	second, err := svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: artifactURL,
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	require.NoError(t, err)
	require.ElementsMatch(t, first, second)

	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)
}

// The channel key includes the package os/arch: two builds for different
// platforms never cancel each other.
func TestSupersedeIsPerPlatform(t *testing.T) {
	cases := loadGoldenCases(t)
	amd64 := cases[nightlyArtifact].ArtifactConfig
	arm64 := make(map[string]any, len(amd64))
	for key, value := range amd64 {
		arm64[key] = value
	}
	arm64["net.pingcap.tibuild.architecture"] = "arm64"

	artifact := &supersedeArtifact{
		config:  amd64,
		digest:  "sha256:8888888888888888888888888888888888888888888888888888888888888888",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()

	first, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, first, 2)

	*clock = clock.Add(time.Minute)
	artifact.config = arm64
	artifact.digest = "sha256:9999999999999999999999999999999999999999999999999999999999999999"
	artifact.created = *clock
	second, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, second, 2)

	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 2, count, "different platforms are different channels")
}

// The dedupe key includes the target: prod and staging never cancel each other.
func TestSupersedeIsPerTarget(t *testing.T) {
	cases := loadGoldenCases(t)
	artifact := &supersedeArtifact{
		config:  cases[nightlyArtifact].ArtifactConfig,
		digest:  "sha256:2222222222222222222222222222222222222222222222222222222222222222",
		created: time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC),
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()
	version := "v9.0.0-beta.2.pre-nightly"

	prodIDs, err := svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: nightlyArtifact,
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	require.NoError(t, err)
	require.Len(t, prodIDs, 1)

	*clock = clock.Add(time.Minute)
	artifact.digest = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
	artifact.created = *clock
	stagingIDs, err := svc.RequestToPublish(ctx, &publish.RequestToPublishPayload{
		ArtifactURL: nightlyArtifact,
		TiupMirror:  delivery.TiUPMirrorStaging,
		Version:     &version,
	})
	require.NoError(t, err)
	require.Len(t, stagingIDs, 1)

	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 2, count, "different targets are different channels")
}

// A delayed retry of an older artifact must not replace a newer queued unit.
func TestSupersedeIgnoresOlderArtifact(t *testing.T) {
	cases := loadGoldenCases(t)
	firstCreated := time.Date(2026, 10, 30, 11, 0, 0, 0, time.UTC)
	artifact := &supersedeArtifact{
		config:  cases[nightlyArtifact].ArtifactConfig,
		digest:  "sha256:4444444444444444444444444444444444444444444444444444444444444444",
		created: firstCreated,
	}
	svc, clock := newSupersedeService(t, map[string]*supersedeArtifact{nightlyArtifact: artifact})
	ctx := context.Background()

	first, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	*clock = clock.Add(time.Minute)
	artifact.digest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
	artifact.created = firstCreated.Add(-time.Hour)

	second, err := svc.DeliveryByRules(ctx, &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.ElementsMatch(t, first, second)

	count, err := svc.db.PublishUnit.Query().Count(ctx)
	require.NoError(t, err)
	require.Equal(t, 1, count)
}

// The layer digest is the package sha256, so an identical remote package is
// recognised without downloading the layer.
func TestPublishOneSkipsDownloadWhenLayerMatchesRemote(t *testing.T) {
	content := []byte("payload")
	sum := sha256.Sum256(content)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(content)
	}))
	defer server.Close()

	pkg := tiupTestPackage
	runner := &tiupCLI{
		bin:  writeFakeTiup(t, "#!/bin/sh\nexit 0\n"),
		http: server.Client(),
		download: func(ctx context.Context, artifactURL, digest, file string) (string, string, error) {
			t.Fatal("an identical remote package must not be downloaded")
			return "", "", nil
		},
		layerDigest: func(ctx context.Context, artifactURL, digest, file string) (string, error) {
			return hex.EncodeToString(sum[:]), nil
		},
	}

	result, err := runner.PublishOne(context.Background(), TiUPStepPayload{
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Digest:      "sha256:6666666666666666666666666666666666666666666666666666666666666666",
		Packages:    []delivery.TiUPPackage{pkg},
	}, pkg)
	require.NoError(t, err)
	require.Equal(t, true, result[tiupResultSkipped])
}
