package impl

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/static"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/registry"
)

// TestCopyImageAgainstRealRegistry proves the two properties the design leans
// on, against a real registry: the copy writes the pinned digest (not whatever
// the source tag points at now) and a second attempt sends nothing.
func TestCopyImageAgainstRealRegistry(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping registry integration test in short mode")
	}
	ctx := context.Background()

	container, err := registry.Run(ctx, "registry:2.8.3")
	require.NoError(t, err)
	t.Cleanup(func() {
		require.NoError(t, testcontainers.TerminateContainer(container))
	})

	host, err := container.HostAddress(ctx)
	require.NoError(t, err)

	srcRepo := host + "/src/widget"
	firstDigest := pushSyntheticImage(t, srcRepo+":v8.5.0", "first")
	secondDigest := pushSyntheticImage(t, srcRepo+":v8.5.0-moved", "second")
	require.NotEqual(t, firstDigest, secondDigest)

	logger := zerolog.Nop()
	svc := &publishsrvc{logger: &logger, images: craneImageOps{}}

	payload := ImageCopyPayload{
		Source: srcRepo + "@" + firstDigest,
		Digest: firstDigest,
		Dest:   host + "/dst/widget",
		Tags:   []string{"v8.5.0", "nightly"},
	}

	result, err := svc.copyImage(ctx, payload)
	require.NoError(t, err)
	require.Len(t, result.Copied, 2)
	require.Empty(t, result.Skipped)

	for _, tag := range payload.Tags {
		digest, err := crane.Digest(payload.Dest + ":" + tag)
		require.NoError(t, err)
		require.Equal(t, firstDigest, digest, "tag %s", tag)
	}

	// A second attempt has nothing to do.
	result, err = svc.copyImage(ctx, payload)
	require.NoError(t, err)
	require.Empty(t, result.Copied)
	require.Len(t, result.Skipped, 2)

	// The source tag moved to another image after planning; the plan still
	// carries the old digest, and a fresh destination receives that digest.
	movedDigest, err := crane.Digest(srcRepo + ":v8.5.0-moved")
	require.NoError(t, err)
	require.Equal(t, secondDigest, movedDigest)

	pinned := payload
	pinned.Dest = host + "/dst/pinned"
	result, err = svc.copyImage(ctx, pinned)
	require.NoError(t, err)
	require.Len(t, result.Copied, 2)
	digest, err := crane.Digest(pinned.Dest + ":v8.5.0")
	require.NoError(t, err)
	require.Equal(t, firstDigest, digest, "the copy uses the planned digest, not the current tag")

	// Overwriting a tag whose digest differs is allowed: the tag is supposed to
	// point at the new digest.
	overwrite := ImageCopyPayload{
		Source: srcRepo + "@" + secondDigest,
		Digest: secondDigest,
		Dest:   payload.Dest,
		Tags:   []string{"v8.5.0"},
	}
	result, err = svc.copyImage(ctx, overwrite)
	require.NoError(t, err)
	require.Len(t, result.Copied, 1)
	digest, err = crane.Digest(payload.Dest + ":v8.5.0")
	require.NoError(t, err)
	require.Equal(t, secondDigest, digest)
}

// pushSyntheticImage pushes a one-layer image built in memory, so the test does
// not depend on an upstream registry.
func pushSyntheticImage(t *testing.T, ref, content string) string {
	t.Helper()

	layer := static.NewLayer([]byte(fmt.Sprintf("%s-%d", content, time.Now().UnixNano())),
		"application/vnd.docker.image.rootfs.diff.tar.gzip")

	image, err := mutate.AppendLayers(empty.Image, layer)
	require.NoError(t, err)

	require.NoError(t, crane.Push(image, ref))

	digest, err := crane.Digest(ref)
	require.NoError(t, err)
	require.NotEmpty(t, digest)
	return digest
}
