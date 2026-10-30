package impl

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func newTiUPServiceForTest(t *testing.T, cfg config.Publish) *publishsrvc {
	t.Helper()
	svc, _ := newFlowTestService(t)
	svc.publishCfg = cfg
	cases := loadGoldenCases(t)
	svc.fetchConfig = func(ctx context.Context, artifactURL string) (map[string]any, string, error) {
		c, ok := cases[artifactURL]
		if !ok {
			return nil, "", errors.New("unexpected artifact " + artifactURL)
		}
		return c.ArtifactConfig, "sha256:1111111111111111111111111111111111111111111111111111111111111111", nil
	}
	return svc
}

const nightlyArtifact = "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:master_linux_amd64"

// The nightly artifact is published to both mirrors, prod first: the staging
// step starts blocked and depends on the prod step.
func TestDeliveryByRulesQueuesProdThenStaging(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	ids, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, ids, 2)

	steps, err := svc.db.PublishStep.Query().
		Where(entpublishstep.IDIn(ids...)).
		Order(ent.Asc(entpublishstep.FieldCreatedAt)).
		All(context.Background())
	require.NoError(t, err)
	require.Len(t, steps, 2)

	byTarget := map[string]*ent.PublishStep{}
	for _, step := range steps {
		byTarget[step.Target] = step
	}
	prod, staging := byTarget[delivery.TiUPTargetProd], byTarget[delivery.TiUPTargetStaging]
	require.NotNil(t, prod)
	require.NotNil(t, staging)
	require.Equal(t, PublishStepStatePending, prod.State)
	require.Empty(t, prod.DependsOn)
	require.Equal(t, PublishStepStateBlocked, staging.State)
	require.Equal(t, prod.ID, staging.DependsOn)

	payload, err := decodeTiUPStepPayload(staging.Payload)
	require.NoError(t, err)
	require.Equal(t, "http://tiup.pingcap.net:8988", payload.MirrorURL)
	require.Len(t, payload.Packages, 4, "the nightly tidb artifact carries four packages")
	require.True(t, payload.Packages[0].Version == "v9.0.0-beta.2.pre-nightly")
}

// Mirrors can be configured to run in parallel by emptying the dependency map.
func TestDeliveryByRulesCanRunMirrorsInParallel(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{TiUPDependencies: map[string]string{}})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	_, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)

	steps, err := svc.db.PublishStep.Query().All(context.Background())
	require.NoError(t, err)
	require.Len(t, steps, 2)
	for _, step := range steps {
		require.Equal(t, PublishStepStatePending, step.State)
		require.Empty(t, step.DependsOn)
	}
}

// An artifact that no rule matches is a successful no-op, without a registry
// call.
func TestDeliveryByRulesNoMatchIsEmpty(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	ids, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{
		ArtifactURL: "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_windows_amd64",
	})
	require.NoError(t, err)
	require.Empty(t, ids)

	count, err := svc.db.PublishUnit.Query().Count(context.Background())
	require.NoError(t, err)
	require.Zero(t, count)
}

// The explicit-mirror path honours the version override.
func TestRequestToPublishSingleMirrorWithVersionOverride(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	version := "v0.0.1-forced"
	ids, err := svc.RequestToPublish(context.Background(), &publish.RequestToPublishPayload{
		ArtifactURL: "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		TiupMirror:  delivery.TiUPMirrorProd,
		Version:     &version,
	})
	require.NoError(t, err)
	require.Len(t, ids, 1)

	step, err := svc.db.PublishStep.Get(context.Background(), ids[0])
	require.NoError(t, err)
	require.Equal(t, delivery.TiUPTargetProd, step.Target)

	payload, err := decodeTiUPStepPayload(step.Payload)
	require.NoError(t, err)
	require.Equal(t, "http://tiup.pingcap.net:8987", payload.MirrorURL)
	for _, pkg := range payload.Packages {
		require.Equal(t, version, pkg.Version)
	}
}

// The legacy status endpoint accepts both unit and step ids, and keeps the
// legacy five-state vocabulary.
func TestQueryPublishingStatusAcceptsUnitAndStepIDs(t *testing.T) {
	svc := newTiUPServiceForTest(t, config.Publish{})
	svc.planner = &publishPlanner{rules: goldenRulesLoader(t)}

	ids, err := svc.DeliveryByRules(context.Background(), &publish.DeliveryByRulesPayload{ArtifactURL: nightlyArtifact})
	require.NoError(t, err)
	require.Len(t, ids, 2)

	state, err := svc.QueryPublishingStatus(context.Background(), &publish.QueryPublishingStatusPayload{RequestID: ids[0]})
	require.NoError(t, err)
	require.Equal(t, publish.TiUPRequestState("queued"), state)

	state, err = svc.QueryPublishingStatus(context.Background(), &publish.QueryPublishingStatusPayload{RequestID: ids[1]})
	require.NoError(t, err)
	require.Equal(t, publish.TiUPRequestState("queued"), state)

	_, err = svc.QueryPublishingStatus(context.Background(), &publish.QueryPublishingStatusPayload{RequestID: "00000000-0000-4000-8000-000000000000"})
	var fault *publish.TiUPFaultError
	require.ErrorAs(t, err, &fault)
	require.Equal(t, "fault", fault.Name)
	require.True(t, fault.Fault)
	require.Contains(t, fault.Message, "request ID not found")
}
