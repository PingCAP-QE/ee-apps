package logging

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

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

func TestUploaderRunUploadsAndReset(t *testing.T) {
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
	ts := httptest.NewServer(logserver.New(logserver.Config{Store: store, MaxBuildBytes: 1024 * 1024}).Handler())
	defer ts.Close()

	uploader := &Uploader{
		Log:      log,
		Client:   ts.Client(),
		URL:      ts.URL + "/v1/logs/ee-cd/macbuild-abc",
		Interval: 10 * time.Millisecond,
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- uploader.Run(ctx) }()

	deadline := time.Now().Add(2 * time.Second)
	for {
		resp, getErr := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc")
		if getErr == nil {
			body, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if strings.Contains(string(body), "hello") {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("uploader did not upload the log within 2s")
		}
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("uploader run: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("uploader did not stop after cancellation")
	}

	if err := uploader.Reset(context.Background()); err != nil {
		t.Fatalf("reset: %v", err)
	}
	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc")
	if err != nil {
		t.Fatalf("get after reset: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 after reset, got %d", resp.StatusCode)
	}
}

func TestUploaderReportsServerErrors(t *testing.T) {
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

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer ts.Close()

	uploader := &Uploader{Log: log, Client: ts.Client(), URL: ts.URL + "/v1/logs/ee-cd/macbuild-abc"}
	if _, err := uploader.Finalize(context.Background()); err == nil {
		t.Fatal("expected finalize to fail when the server returns 500")
	}
	if err := uploader.Reset(context.Background()); err == nil {
		t.Fatal("expected reset to fail when the server returns 500")
	}
}
