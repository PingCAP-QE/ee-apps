package impl

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// newTiUPTestClock returns a publish service on an in-memory database with a
// controlled clock and the TiUP lane seeded.
func newFlowTestService(t *testing.T) (*publishsrvc, *time.Time) {
	t.Helper()
	logger := zerolog.Nop()
	svc := newPublishService(&logger, serverlessTestDB(t), &config.Service{})
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	clock := &now
	svc.nowFn = func() time.Time { return *clock }
	svc.lane.nowFn = func() time.Time { return *clock }
	require.NoError(t, svc.lane.ensure(context.Background()))
	return svc, clock
}

// createTiUPStep persists a queued TiUP unit with one pending step.
func createTiUPStep(t *testing.T, svc *publishsrvc, target string, payload TiUPStepPayload) (*ent.PublishUnit, *ent.PublishStep) {
	t.Helper()
	ctx := context.Background()
	now := svc.now()

	unit, err := svc.db.PublishUnit.Create().
		SetID(svc.id()).
		SetKind(PublishKindTiUP).
		SetArtifactURL(payload.ArtifactURL).
		SetState(PublishStateQueued).
		SetCreatedAt(now).
		Save(ctx)
	require.NoError(t, err)

	raw, err := payloadMap(payload)
	require.NoError(t, err)
	step, err := svc.db.PublishStep.Create().
		SetID(svc.id()).
		SetUnitID(unit.ID).
		SetTarget(target).
		SetState(PublishStepStatePending).
		SetPayload(raw).
		SetCreatedAt(now).
		Save(ctx)
	require.NoError(t, err)
	return unit, step
}

// createDependentUnit persists a TiUP unit whose second target waits for the
// first, like prod -> staging.
func createDependentUnit(t *testing.T, svc *publishsrvc, first, second string) (*ent.PublishUnit, *ent.PublishStep, *ent.PublishStep) {
	t.Helper()
	ctx := context.Background()
	now := svc.now()

	unit, err := svc.db.PublishUnit.Create().
		SetID(svc.id()).
		SetKind(PublishKindTiUP).
		SetArtifactURL("example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64").
		SetState(PublishStateQueued).
		SetCreatedAt(now).
		Save(ctx)
	require.NoError(t, err)

	firstStep, err := svc.db.PublishStep.Create().
		SetID(svc.id()).
		SetUnitID(unit.ID).
		SetTarget(first).
		SetState(PublishStepStatePending).
		SetPayload(map[string]any{}).
		SetCreatedAt(now).
		Save(ctx)
	require.NoError(t, err)

	secondStep, err := svc.db.PublishStep.Create().
		SetID(svc.id()).
		SetUnitID(unit.ID).
		SetTarget(second).
		SetState(PublishStepStateBlocked).
		SetDependsOn(firstStep.ID).
		SetPayload(map[string]any{}).
		SetCreatedAt(now).
		Save(ctx)
	require.NoError(t, err)

	return unit, firstStep, secondStep
}

// goldenRulesLoader writes the TiUP rules snapshot to a temp file.
func goldenRulesLoader(t *testing.T) *rulesLoader {
	t.Helper()
	content, err := os.ReadFile("../../../pkg/delivery/testdata/tiup_delivery_golden.yaml")
	require.NoError(t, err)
	var doc struct {
		Rules map[string][]delivery.TiUPRule `yaml:"rules"`
	}
	require.NoError(t, yaml.Unmarshal(content, &doc))
	require.NotEmpty(t, doc.Rules)

	raw, err := yaml.Marshal(map[string]any{"tiup_publish_rules": doc.Rules})
	require.NoError(t, err)
	path := filepath.Join(t.TempDir(), "delivery.yaml")
	require.NoError(t, os.WriteFile(path, raw, 0o644))
	return &rulesLoader{path: path}
}

// newTiUPTestService returns a service whose TiUP runner downloads a fixed
// payload, so the executor can be exercised without a registry.
func newTiUPTestService(t *testing.T, mirrorURL string) *publishsrvc {
	t.Helper()
	svc, _ := newFlowTestService(t)
	svc.tiup = &tiupCLI{
		bin:  writeFakeTiup(t, "#!/bin/sh\nexit 0\n"),
		http: http.DefaultClient,
		download: func(ctx context.Context, artifactURL, digest, file string) (string, string, error) {
			return writeTempFile(t, file, []byte("payload"))
		},
	}
	_ = mirrorURL
	return svc
}

// writeFakeTiup installs an executable script that stands in for `tiup`.
func writeFakeTiup(t *testing.T, script string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tiup")
	require.NoError(t, os.WriteFile(path, []byte(script), 0o755))
	return path
}

// writeTempFile writes content to a temp file and returns its path and sha256.
func writeTempFile(t *testing.T, name string, content []byte) (string, string, error) {
	t.Helper()
	file, err := os.CreateTemp(t.TempDir(), "artifact-*")
	require.NoError(t, err)
	_, err = file.Write(content)
	require.NoError(t, err)
	require.NoError(t, file.Close())
	sum := sha256.Sum256(content)
	_ = name
	return file.Name(), hex.EncodeToString(sum[:]), nil
}

// goldenCase is the subset of the recorded TiUP corpus the service tests need.
type goldenCase struct {
	ArtifactURL    string         `yaml:"artifact_url"`
	ArtifactConfig map[string]any `yaml:"artifact_config"`
}

// loadGoldenCases reads the hermetic corpus recorded from the old publisher.
func loadGoldenCases(t *testing.T) map[string]goldenCase {
	t.Helper()
	content, err := os.ReadFile("../../../pkg/delivery/testdata/tiup_delivery_golden.yaml")
	require.NoError(t, err)

	var doc struct {
		Cases []goldenCase `yaml:"cases"`
	}
	require.NoError(t, yaml.Unmarshal(content, &doc))
	require.NotEmpty(t, doc.Cases)

	out := make(map[string]goldenCase, len(doc.Cases))
	for _, c := range doc.Cases {
		out[c.ArtifactURL] = c
	}
	return out
}
