package impl

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// A publish source is request-controlled and its URL ends up in step errors,
// which are persisted and rendered. Credentials in the query must not travel
// with it.
func TestDownloadHTTPSanitizesErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(srv.Close)

	c := &tiupCLI{http: srv.Client()}

	_, _, err := c.downloadHTTP(context.Background(), srv.URL+"/pkg.tar.gz?X-Amz-Signature=secret-token")
	require.Error(t, err)
	require.NotContains(t, err.Error(), "secret-token", "the signed query must be dropped from the error")
	require.Contains(t, err.Error(), "status 404")
}

// An oversized body must fail instead of being streamed to disk.
func TestDownloadHTTPRejectsOversizedBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(make([]byte, 4096))
	}))
	t.Cleanup(srv.Close)

	original := maxPublishDownloadBytes
	maxPublishDownloadBytes = 1024
	t.Cleanup(func() { maxPublishDownloadBytes = original })

	c := &tiupCLI{http: srv.Client()}

	_, _, err := c.downloadHTTP(context.Background(), srv.URL+"/pkg.tar.gz")
	require.ErrorContains(t, err, "exceeds 1024 bytes")
}

func TestDownloadHTTPAcceptsBodyUnderLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("package-bytes"))
	}))
	t.Cleanup(srv.Close)

	original := maxPublishDownloadBytes
	maxPublishDownloadBytes = 1024
	t.Cleanup(func() { maxPublishDownloadBytes = original })

	c := &tiupCLI{http: srv.Client()}

	file, sum, err := c.downloadHTTP(context.Background(), srv.URL+"/pkg.tar.gz")
	require.NoError(t, err)
	t.Cleanup(func() { _ = os.Remove(file) })

	got, err := os.ReadFile(file)
	require.NoError(t, err)
	require.Equal(t, "package-bytes", string(got))
	require.NotEmpty(t, sum)
	require.False(t, strings.ContainsRune(sum, 'z'))
}
