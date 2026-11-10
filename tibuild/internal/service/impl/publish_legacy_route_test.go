package impl

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestLegacyResetRateLimitRouteIsGone pins the removal of the compatibility
// no-op. Nightly convergence is a desired state in publish_desired, so a caller
// that still resets a rate limit has nothing to reset; it must learn that from
// the API instead of receiving a silent 200.
func TestLegacyResetRateLimitRouteIsGone(t *testing.T) {
	server, _ := newTiUPHTTPTestServer(t)

	resp, err := http.Post(server.URL+"/api/v2/publish/tiup/reset-rate-limit", "application/json", nil)
	require.NoError(t, err)
	defer resp.Body.Close()

	require.Equal(t, http.StatusNotFound, resp.StatusCode)
}
