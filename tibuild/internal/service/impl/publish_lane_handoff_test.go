package impl

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// A two-step ordered unit is executed by two workers. After the prod step
// succeeds the unit is still running, so the unit-terminal release does not
// fire; the step-terminal release must free the lane for the staging step.
func TestStepCompletionReleasesLane(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("payload"))
	}))
	defer server.Close()

	svc := newTiUPServiceForTest(t, config.Publish{
		TiUPMirrors: map[string]string{"staging": server.URL, "prod": server.URL},
	})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}
	svc.tiup = &tiupCLI{
		bin:  writeFakeTiup(t, "#!/bin/sh\nexit 0\n"),
		http: server.Client(),
		download: func(ctx context.Context, artifactURL, digest, file string) (string, string, error) {
			return writeTempFile(t, file, []byte("payload"))
		},
	}

	ids, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, ids, 2)

	claimed, err := svc.claimStep(context.Background())
	require.NoError(t, err)
	require.NotNil(t, claimed)
	require.Equal(t, delivery.TiUPTargetProd, claimed.Target)
	svc.executeStep(context.Background(), claimed)

	// The prod step succeeded but the unit is still running (staging pending).
	steps, err := svc.db.PublishStep.Query().
		Where(entpublishstep.UnitID(claimed.UnitID)).
		All(context.Background())
	require.NoError(t, err)
	var staging *ent.PublishStep
	for _, step := range steps {
		if step.Target == delivery.TiUPTargetStaging {
			staging = step
		}
	}
	require.NotNil(t, staging)
	require.Equal(t, PublishStepStatePending, staging.State)
	require.Equal(t, PublishStateQueued, unitByID(t, svc, claimed.UnitID).State, "the unit is not terminal yet")

	// Another unit must be able to take the lane immediately, not after 9m.
	ok, err := svc.lane.Acquire(context.Background(), "another-unit", "worker-other", time.Minute)
	require.NoError(t, err)
	require.True(t, ok, "the lane must be released when the step finishes")
}

// Priority aging lets a long-waiting nightly step overtake new API steps, while
// release priority still wins.
func TestClaimAgingPrefersLongWaitingNightly(t *testing.T) {
	svc := newPublishTestService(t, publishTestRules, nil)
	ctx := context.Background()
	now := svc.now()

	create := func(priority int, createdAt time.Time) *ent.PublishUnit {
		unit, err := svc.db.PublishUnit.Create().
			SetID(svc.id()).
			SetKind(PublishKindImageCopy).
			SetArtifactURL("example.com/hub/pingcap/widget/image:v8.5.0").
			SetState(PublishStateQueued).
			SetPriority(priority).
			SetCreatedAt(createdAt).
			Save(ctx)
		require.NoError(t, err)
		_, err = svc.db.PublishStep.Create().
			SetID(svc.id()).
			SetUnitID(unit.ID).
			SetTarget("docker.io").
			SetState(PublishStepStatePending).
			SetPriority(priority).
			SetPayload(map[string]any{}).
			SetCreatedAt(createdAt).
			Save(ctx)
		require.NoError(t, err)
		return unit
	}

	nightly := create(10, now.Add(-20*time.Minute))
	api := create(20, now)
	release := create(30, now)

	first, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.Equal(t, release.ID, first.UnitID, "release priority still wins")

	second, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.Equal(t, nightly.ID, second.UnitID, "the aged nightly step beats the new API step")

	third, err := svc.claimStep(ctx)
	require.NoError(t, err)
	require.Equal(t, api.ID, third.UnitID)
}

// The layer is streamed to disk, never buffered whole in memory.
func TestWriteTempFromReaderKeepsSHAAndSize(t *testing.T) {
	content := []byte("streamed layer payload")
	sum := sha256.Sum256(content)
	path, digest, err := writeTempFromReader(bytes.NewReader(content), int64(len(content)))
	require.NoError(t, err)
	defer os.Remove(path)

	require.Equal(t, hex.EncodeToString(sum[:]), digest)
	data, err := os.ReadFile(path)
	require.NoError(t, err)
	require.Equal(t, content, data)

	_, _, err = writeTempFromReader(bytes.NewReader(content), int64(len(content)+1))
	require.Error(t, err, "a truncated stream must be rejected")
}
