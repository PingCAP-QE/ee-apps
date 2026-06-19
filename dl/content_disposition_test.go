package dl

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

// TestContentDisposition verifies that the Content-Disposition header
// includes both filename (ASCII fallback) and filename* (UTF-8 encoded).
func TestContentDisposition(t *testing.T) {
	tests := []struct {
		name     string
		filename string
		want     string
	}{
		{
			name:     "ASCII filename",
			filename: "cdc-v8.5.5-linux-amd64.tar.gz",
			want:     `attachment; filename="cdc-v8.5.5-linux-amd64.tar.gz"; filename*=UTF-8''cdc-v8.5.5-linux-amd64.tar.gz`,
		},
		{
			name:     "ASCII filename with spaces",
			filename: "my file.tar.gz",
			want:     `attachment; filename="my file.tar.gz"; filename*=UTF-8''my+file.tar.gz`,
		},
		{
			name:     "Chinese filename",
			filename: "测试文件.tar.gz",
			want:     `attachment; filename="____.tar.gz"; filename*=UTF-8''%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6.tar.gz`,
		},
		{
			name:     "filename with special chars",
			filename: "file+with+plus.tar.gz",
			want:     `attachment; filename="file+with+plus.tar.gz"; filename*=UTF-8''file%2Bwith%2Bplus.tar.gz`,
		},
		{
			name:     "SHA256 filename",
			filename: "cdc-v8.5.5-linux-amd64.tar.gz.sha256",
			want:     `attachment; filename="cdc-v8.5.5-linux-amd64.tar.gz.sha256"; filename*=UTF-8''cdc-v8.5.5-linux-amd64.tar.gz.sha256`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ContentDisposition(tt.filename)
			if got != tt.want {
				t.Errorf("ContentDisposition(%q)\n  got:  %q\n  want: %q", tt.filename, got, tt.want)
			}
		})
	}
}

// TestContentDispositionHTTPResponse verifies that the HTTP response
// includes the correct Content-Disposition header format.
func TestContentDispositionHTTPResponse(t *testing.T) {
	// Create a test server that uses our ContentDisposition function
	mux := http.NewServeMux()
	mux.HandleFunc("/test", func(w http.ResponseWriter, r *http.Request) {
		filename := r.URL.Query().Get("file")
		w.Header().Set("Content-Disposition", ContentDisposition(filename))
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
	})

	server := httptest.NewServer(mux)
	defer server.Close()

	tests := []struct {
		name     string
		filename string
	}{
		{
			name:     "ASCII filename",
			filename: "test.tar.gz",
		},
		{
			name:     "filename with special chars",
			filename: "file+with+plus.tar.gz",
		},
		{
			name:     "SHA256 filename",
			filename: "test.tar.gz.sha256",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp, err := http.Get(server.URL + "/test?file=" + url.QueryEscape(tt.filename))
			if err != nil {
				t.Fatalf("failed to make request: %v", err)
			}
			defer resp.Body.Close()

			cd := resp.Header.Get("Content-Disposition")
			t.Logf("Content-Disposition: %s", cd)

			// Verify the header contains both filename and filename*
			if !strings.Contains(cd, "filename=") {
				t.Error("Content-Disposition missing 'filename=' parameter")
			}
			if !strings.Contains(cd, "filename*=") {
				t.Error("Content-Disposition missing 'filename*=' parameter")
			}

			// Verify the filename* parameter uses UTF-8 encoding
			expectedFilenameStar := "filename*=UTF-8''" + url.QueryEscape(tt.filename)
			if !strings.Contains(cd, expectedFilenameStar) {
				t.Errorf("Content-Disposition missing expected filename*=%q, got: %s",
					expectedFilenameStar, cd)
			}

			// Verify the filename parameter is ASCII-safe (quoted)
			expectedFilename := `filename="` + tt.filename + `"`
			if !strings.Contains(cd, expectedFilename) {
				t.Errorf("Content-Disposition missing expected %q, got: %s",
					expectedFilename, cd)
			}
		})
	}
}

// TestContentDispositionWgetCompatibility verifies that the Content-Disposition
// header format is compatible with wget --content-disposition.
// This test documents the expected behavior that wget will use the filename
// parameter (not filename*) when --content-disposition is used.
func TestContentDispositionWgetCompatibility(t *testing.T) {
	filename := "cdc-v8.5.5-release.3-20260127-69f3866-linux-amd64.tar.gz"
	cd := ContentDisposition(filename)

	// The filename parameter should be present and ASCII-safe
	// This is what wget --content-disposition will use
	if !strings.Contains(cd, `filename="`+filename+`"`) {
		t.Errorf("Content-Disposition should contain ASCII-safe filename for wget compatibility, got: %s", cd)
	}

	// The filename* parameter should also be present for modern clients
	if !strings.Contains(cd, "filename*=UTF-8''"+url.QueryEscape(filename)) {
		t.Errorf("Content-Disposition should contain UTF-8 encoded filename*, got: %s", cd)
	}
}
