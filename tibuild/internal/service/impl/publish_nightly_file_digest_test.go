package impl

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishdesired"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func testDigest(fill string) digest.Digest {
	return digest.Digest("sha256:" + strings.Repeat(fill, 64))
}

// TestLayerSHA256IndexesFilesByTitle checks the pure extraction step: the
// tarball sha256 is the layer digest, keyed by the OCI layer title, which is
// what the mirror records for the published file.
func TestLayerSHA256IndexesFilesByTitle(t *testing.T) {
	manifest := ocispec.Manifest{Layers: []ocispec.Descriptor{
		{
			MediaType:   "application/gzip",
			Digest:      testDigest("a"),
			Annotations: map[string]string{ocispec.AnnotationTitle: "pd-v9.0.0-nightly-linux-amd64.tar.gz"},
		},
		{
			MediaType:   "application/gzip",
			Digest:      testDigest("b"),
			Annotations: map[string]string{ocispec.AnnotationTitle: "pd-v9.0.0-nightly-linux-arm64.tar.gz"},
		},
		{
			// A layer without a title cannot be matched to a package file.
			MediaType: "application/gzip",
			Digest:    testDigest("c"),
		},
	}}

	got := layerSHA256(manifest)

	require.Equal(t, map[string]string{
		"pd-v9.0.0-nightly-linux-amd64.tar.gz": strings.Repeat("a", 64),
		"pd-v9.0.0-nightly-linux-arm64.tar.gz": strings.Repeat("b", 64),
	}, got, "the map must be keyed by title with the layer sha256, no sha256: prefix")
}

// TestEnqueueNightlyPlansRecordsFileDigest pins the contract the verification
// relies on: the desired payload carries the sha256 of the package's own
// tarball, so publication can be checked against the mirror metadata instead of
// the (constant) nightly version string.
func TestEnqueueNightlyPlansRecordsFileDigest(t *testing.T) {
	const (
		artifact = "hub.pingcap.net/pingcap/pd/package:master_linux_amd64"
		version  = "v9.0.0-beta.2.pre-nightly"
		file     = "pd-v9.0.0-beta.2.pre-318-g96033a26b-linux-amd64.tar.gz"
	)

	svc, clock := newFlowTestService(t)
	nightly := nightlyTestConfig(true, delivery.TiUPTargetStaging)
	svc.publishCfg = config.Publish{Nightly: nightly}
	svc.desired = newDesiredStore(svc.db, nightly)
	svc.desired.nowFn = func() time.Time { return *clock }

	plans := []delivery.TiUPPlan{{
		Mirror:  delivery.TiUPMirrorStaging,
		Target:  delivery.TiUPTargetStaging,
		Version: version,
		Nightly: true,
		Packages: []delivery.TiUPPackage{{
			Name: "pd", Version: version, OS: "linux", Arch: "amd64",
			EntryPoint: "pd-server", File: file,
		}},
	}}

	t.Run("known file", func(t *testing.T) {
		require.NoError(t, svc.enqueueNightlyPlans(context.Background(), artifact, "sha256:manifest",
			map[string]string{file: strings.Repeat("a", 64)}, plans))

		row, err := svc.db.PublishDesired.Query().
			Where(publishdesired.Target(delivery.TiUPTargetStaging), publishdesired.Pkg("pd")).Only(context.Background())
		require.NoError(t, err)

		payload, err := decodeTiUPStepPayload(row.DesiredPayload)
		require.NoError(t, err)
		require.Equal(t, strings.Repeat("a", 64), payload.FileDigest,
			"desired payload must carry the tarball sha256 the mirror will record")
		require.Equal(t, "sha256:manifest", payload.Digest,
			"the manifest digest keeps pinning the artifact")
		require.Equal(t, version, payload.NightlyVersion)
	})

	t.Run("file missing from the manifest", func(t *testing.T) {
		require.NoError(t, svc.enqueueNightlyPlans(context.Background(), artifact, "sha256:manifest",
			map[string]string{"some-other-file.tar.gz": strings.Repeat("b", 64)}, plans))

		row, err := svc.db.PublishDesired.Query().
			Where(publishdesired.Target(delivery.TiUPTargetStaging), publishdesired.Pkg("pd")).Only(context.Background())
		require.NoError(t, err)

		payload, err := decodeTiUPStepPayload(row.DesiredPayload)
		require.NoError(t, err)
		require.Empty(t, payload.FileDigest,
			"an unmatched file must leave the field empty so verification falls back")
	})
}
