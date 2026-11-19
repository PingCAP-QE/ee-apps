package impl

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestIsFormalImageTag(t *testing.T) {
	formal := []string{"v8.5.0", "v1.0.0", "v26.3.1"}
	informal := []string{"nightly", "master", "v8.5.0-pre", "v8.5.0-20260101", "v1.2", "v1.2.3.4", "8.5.0", ""}
	for _, tag := range formal {
		require.True(t, isFormalImageTag(tag), "%q must count as a released tag", tag)
	}
	for _, tag := range informal {
		require.False(t, isFormalImageTag(tag), "%q must not count as a released tag", tag)
	}
}

// A released tag is what users already pull, so a plan that would replace it
// must fail instead of copying over it.
func TestCopyImageRefusesToOverwriteReleasedTag(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/pingcap/tidb:v8.5.0": "sha256:existing",
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	_, err := svc.copyImage(context.Background(), ImageCopyPayload{
		Source: "example.com/hub/pingcap/tidb/image:v8.5.0",
		Dest:   "example.com/pingcap/tidb",
		Digest: "sha256:planned",
		Tags:   []string{"v8.5.0"},
	})
	require.ErrorContains(t, err, "refusing to overwrite published release tag")
	require.Empty(t, ops.copies, "no copy may be attempted")
}

// Non-formal tags are replaced on purpose: re-publishing nightly means moving
// it forward.
func TestCopyImageAllowsOverwritingNonFormalTag(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/pingcap/tidb:nightly": "sha256:existing",
	}}
	ops.onCopy = func(_, dst string) { ops.digests[dst] = "sha256:planned" }
	svc := newPublishTestService(t, publishTestRules, ops)

	result, err := svc.copyImage(context.Background(), ImageCopyPayload{
		Source: "example.com/hub/pingcap/tidb/image:nightly",
		Dest:   "example.com/pingcap/tidb",
		Digest: "sha256:planned",
		Tags:   []string{"nightly"},
	})
	require.NoError(t, err)
	require.Equal(t, []string{"example.com/pingcap/tidb:nightly"}, result.Copied)
	require.Len(t, ops.copies, 1)
}

// The idempotent case must stay a no-op, not a refusal.
func TestCopyImageSkipsTagThatAlreadyServesThePlan(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/pingcap/tidb:v8.5.0": "sha256:planned",
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	result, err := svc.copyImage(context.Background(), ImageCopyPayload{
		Source: "example.com/hub/pingcap/tidb/image:v8.5.0",
		Dest:   "example.com/pingcap/tidb",
		Digest: "sha256:planned",
		Tags:   []string{"v8.5.0"},
	})
	require.NoError(t, err)
	require.Empty(t, ops.copies)
	require.Equal(t, []string{"example.com/pingcap/tidb:v8.5.0"}, result.Skipped)
}
