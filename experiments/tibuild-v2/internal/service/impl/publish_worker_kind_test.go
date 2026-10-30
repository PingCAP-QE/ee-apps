package impl

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// A TiUP-only worker must pass startup validation: its targets are mirrors, not
// registry hosts from the delivery rules.
func TestValidatePublishTargetsSkipsTiUPWorkers(t *testing.T) {
	cfg := &config.Service{
		Worker: config.Worker{Kinds: []string{PublishKindTiUP}, Targets: []string{delivery.TiUPTargetStaging}},
		Publish: config.Publish{
			// Deliberately unreadable: a TiUP worker must not need the rules.
			DeliveryConfigPath: "/nonexistent/delivery.yaml",
		},
	}
	require.NoError(t, ValidatePublishTargets(cfg))
}

// An image worker still fails fast on a target the rules do not know.
func TestValidatePublishTargetsChecksImageTargets(t *testing.T) {
	cfg := &config.Service{
		Worker: config.Worker{Kinds: []string{PublishKindImageCopy}, Targets: []string{"docker.io"}},
		Publish: config.Publish{
			DeliveryConfigPath: "/nonexistent/delivery.yaml",
		},
	}
	err := ValidatePublishTargets(cfg)
	require.ErrorIs(t, err, ErrPublishRulesUnavailable)
}

// A TiUP-only worker runs the claim loop: the step is executed without an
// image_copy kind in the configuration.
func TestTiUPOnlyWorkerClaimsSteps(t *testing.T) {
	mirror := &fileMirror{files: map[string][]byte{tiupTestPackage.File: []byte("payload")}}
	server := newMirrorServer(t, mirror)

	svc := newTiUPTestService(t, server.URL)
	svc.workerCfg.Kinds = []string{PublishKindTiUP}
	svc.workerCfg.Targets = []string{delivery.TiUPTargetStaging}
	svc.workerCfg.PollInterval = "10ms"

	unit, step := createTiUPStep(t, svc, delivery.TiUPTargetStaging, TiUPStepPayload{
		Mirror:      delivery.TiUPMirrorStaging,
		MirrorURL:   server.URL,
		ArtifactURL: "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
		Packages:    []delivery.TiUPPackage{tiupTestPackage},
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go svc.Start(ctx)

	require.Eventually(t, func() bool {
		stored, err := svc.db.PublishStep.Get(context.Background(), step.ID)
		return err == nil && stored.State == PublishStepStateSucceeded
	}, 5*time.Second, 20*time.Millisecond, "the tiup worker must execute the step")

	require.Equal(t, PublishStateSucceeded, unitByID(t, svc, unit.ID).State)
}

func newMirrorServer(t *testing.T, mirror *fileMirror) *testServer {
	t.Helper()
	return startMirror(t, mirror)
}
