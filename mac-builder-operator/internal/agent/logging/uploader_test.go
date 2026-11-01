package logging

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"k8s.io/client-go/rest"

	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/logserver"
)

func TestUploaderUploadsAndFinalizes(t *testing.T) {
	t.Parallel()

	log, err := NewBuildLog(t.TempDir(), "ee-cd", "macbuild-abc", 1024*1024)
	if err != nil {
		t.Fatalf("new log: %v", err)
	}
	if _, err := log.CommandWriter().Write([]byte("hello\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := log.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	store := logserver.NewStore(t.TempDir())
	server := logserver.New(logserver.Config{Store: store, MaxBuildBytes: 1024 * 1024})
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()

	uploader := &Uploader{
		Log:    log,
		Client: ts.Client(),
		URL:    ts.URL + "/v1/logs/ee-cd/macbuild-abc",
	}
	meta, err := uploader.Finalize(context.Background())
	if err != nil {
		t.Fatalf("finalize: %v", err)
	}
	if !meta.Complete || meta.Size == 0 {
		t.Fatalf("unexpected finalize meta: %#v", meta)
	}

	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc")
	if err != nil {
		t.Fatalf("get log: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "hello") {
		t.Fatalf("unexpected remote log: %s", body)
	}
}

func TestNewUploaderBuildsServiceProxyURL(t *testing.T) {
	t.Parallel()

	uploader, err := NewUploader(
		&rest.Config{Host: "https://api.example.com/"},
		"ee-cd", "mac-builder-operator-logs", 8080,
		"ee-cd", "macbuild-abc",
		&Log{path: "/tmp/does-not-matter.log"},
	)
	if err != nil {
		t.Fatalf("new uploader: %v", err)
	}
	want := "https://api.example.com/api/v1/namespaces/ee-cd/services/http:mac-builder-operator-logs:8080/proxy/v1/logs/ee-cd/macbuild-abc"
	if uploader.URL != want {
		t.Fatalf("unexpected uploader URL:\nwant %s\n got %s", want, uploader.URL)
	}
}
