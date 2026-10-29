package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthMux(t *testing.T) {
	server := httptest.NewServer(newHealthMux())
	defer server.Close()

	for _, path := range []string{"/livez", "/healthz"} {
		resp, err := http.Get(server.URL + path)
		if err != nil {
			t.Fatalf("GET %s: %v", path, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("GET %s = %d, want 200", path, resp.StatusCode)
		}
	}
}
