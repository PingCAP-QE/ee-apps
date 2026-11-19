package impl

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

// A rollout names a tag of a configured base image. If that tag does not exist
// the ops platform is asked to move a production component onto something that
// was never built, so the request is refused before any call.
func TestUpdateComponentVersionRefusesUnpublishedTag(t *testing.T) {
	const repo = "us.gcr.io/pingcap-public/tidbx/tidb"

	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"prod": {
				APIBaseURL: "http://127.0.0.1:1",
				APIKey:     "secret-key",
				Components: map[string]OpsComponent{
					"tidb": {BaseImage: repo, ClusterType: "SHARED_POOL", GitHubRepo: "pingcap/tidb"},
				},
			},
		},
	}, nil)
	svc.resolveImageTag = func(_ context.Context, ref string) (string, error) {
		require.Equal(t, repo+":v26.3.1-nextgen", ref)
		return "", errors.New("MANIFEST_UNKNOWN")
	}
	_, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: repo + ":v26.3.1-nextgen",
	})
	require.ErrorContains(t, err, "is not readable")
}

// A tag that resolves but is not a nextgen release tag is still skipped: the
// existence check is not a replacement for the shape check.
func TestUpdateComponentVersionStillSkipsNonNextgenTag(t *testing.T) {
	const repo = "us.gcr.io/pingcap-public/tidbx/tidb"

	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"prod": {
				APIBaseURL: "http://127.0.0.1:1",
				APIKey:     "secret-key",
				Components: map[string]OpsComponent{
					"tidb": {BaseImage: repo, ClusterType: "SHARED_POOL", GitHubRepo: "pingcap/tidb"},
				},
			},
		},
	}, nil)

	res, err := svc.UpdateComponentVersionInCloudconfig(context.Background(), &tidbcloud.UpdateComponentVersionInCloudconfigPayload{
		Stage: "prod",
		Image: repo + ":nightly",
	})
	require.NoError(t, err)
	require.Empty(t, res.Tickets)
}

func TestVerifyImageTagExists(t *testing.T) {
	t.Run("an empty digest is refused", func(t *testing.T) {
		svc := &tidbcloudsrvc{resolveImageTag: func(context.Context, string) (string, error) {
			return "  ", nil
		}}
		require.ErrorContains(t, svc.verifyImageTagExists(context.Background(), "example.com/x", "v1"), "empty digest")
	})

	t.Run("a missing resolver is refused rather than assumed", func(t *testing.T) {
		svc := &tidbcloudsrvc{}
		require.ErrorContains(t, svc.verifyImageTagExists(context.Background(), "example.com/x", "v1"), "no image resolver")
	})

	t.Run("a digest passes", func(t *testing.T) {
		svc := &tidbcloudsrvc{resolveImageTag: func(context.Context, string) (string, error) {
			return "sha256:abc", nil
		}}
		require.NoError(t, svc.verifyImageTagExists(context.Background(), "example.com/x", "v1"))
	})
}
