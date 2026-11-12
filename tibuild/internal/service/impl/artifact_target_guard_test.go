package impl

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/stretchr/testify/require"
)

// newTestRegistry serves the distribution API in-process; no Docker needed.
// The host is spelled localhost so go-containerregistry uses plain HTTP.
func newTestRegistry(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(registry.New())
	t.Cleanup(srv.Close)
	return "localhost:" + strings.TrimPrefix(strings.TrimPrefix(srv.URL, "http://"), "127.0.0.1:")
}

func pushRandomImage(t *testing.T, ref string) string {
	t.Helper()
	img, err := random.Image(1024, 1)
	require.NoError(t, err)
	parsed, err := name.ParseReference(ref)
	require.NoError(t, err)
	require.NoError(t, remote.Write(parsed, img))
	digest, err := img.Digest()
	require.NoError(t, err)
	return digest.String()
}

func TestGuardTarget(t *testing.T) {
	host := newTestRegistry(t)
	ctx := context.Background()

	t.Run("an unpublished target may be copied", func(t *testing.T) {
		pushRandomImage(t, host+"/src/absent:v1")
		state, err := guardTarget(ctx, host+"/src/absent:v1", host+"/dst/absent:v1")
		require.NoError(t, err)
		require.Equal(t, targetAbsent, state)
	})

	t.Run("the same digest on the target is a no-op, not a conflict", func(t *testing.T) {
		host := newTestRegistry(t)
		img, err := random.Image(1024, 1)
		require.NoError(t, err)
		for _, ref := range []string{host + "/src/same:v1", host + "/dst/same:v1"} {
			parsed, err := name.ParseReference(ref)
			require.NoError(t, err)
			require.NoError(t, remote.Write(parsed, img))
		}

		state, err := guardTarget(ctx, host+"/src/same:v1", host+"/dst/same:v1")
		require.NoError(t, err)
		require.Equal(t, targetSameDigest, state)
	})

	t.Run("a different digest already on the target is refused", func(t *testing.T) {
		host := newTestRegistry(t)
		pushRandomImage(t, host+"/src/differ:v1")
		pushRandomImage(t, host+"/dst/differ:v1")

		_, err := guardTarget(ctx, host+"/src/differ:v1", host+"/dst/differ:v1")
		require.ErrorContains(t, err, "refusing to overwrite")
	})

	t.Run("a missing source is an error", func(t *testing.T) {
		host := newTestRegistry(t)
		_, err := guardTarget(ctx, host+"/src/missing:v1", host+"/dst/missing:v1")
		require.ErrorContains(t, err, "resolve source image")
	})

	t.Run("an unparsable reference is an error", func(t *testing.T) {
		_, err := guardTarget(ctx, "not a reference", "also not a reference")
		require.ErrorContains(t, err, "parse source image")
	})
}
