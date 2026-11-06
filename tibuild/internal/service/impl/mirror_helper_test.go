package impl

import (
	"net/http/httptest"
	"testing"
)

// testServer wraps httptest.Server so tests can reach .URL.
type testServer = httptest.Server

func startMirror(t *testing.T, mirror *fileMirror) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(mirror.handler())
	t.Cleanup(server.Close)
	return server
}
