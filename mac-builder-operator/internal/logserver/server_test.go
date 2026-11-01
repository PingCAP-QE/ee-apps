package logserver

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestServer(t *testing.T, maxBuildBytes int64) *httptest.Server {
	t.Helper()
	server := New(Config{
		Store:         NewStore(t.TempDir()),
		MaxBuildBytes: maxBuildBytes,
	})
	return httptest.NewServer(server.Handler())
}

func putLogChunk(t *testing.T, baseURL, namespace string, offset int64, data string) *http.Response {
	t.Helper()
	sum := sha256.Sum256([]byte(data))
	req, err := http.NewRequest(http.MethodPut,
		fmt.Sprintf("%s/v1/logs/%s/macbuild-abc?offset=%d", baseURL, namespace, offset),
		strings.NewReader(data))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("X-Chunk-SHA256", hex.EncodeToString(sum[:]))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("put chunk: %v", err)
	}
	return resp
}

func TestServerPutGetMetaFinalize(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 0)
	defer ts.Close()

	resp := putLogChunk(t, ts.URL, "ee-cd", 0, "hello")
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("put status %d: %s", resp.StatusCode, body)
	}
	_ = resp.Body.Close()

	resp = putLogChunk(t, ts.URL, "ee-cd", 5, " world")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("second put status %d", resp.StatusCode)
	}
	_ = resp.Body.Close()

	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc")
	if err != nil {
		t.Fatalf("get log: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if string(body) != "hello world" {
		t.Fatalf("unexpected log: %q", body)
	}

	resp, err = http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc?tail=1")
	if err != nil {
		t.Fatalf("get tail: %v", err)
	}
	body, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if string(body) != "hello world" {
		t.Fatalf("unexpected tail: %q", body)
	}

	resp, err = http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc/meta")
	if err != nil {
		t.Fatalf("get meta: %v", err)
	}
	var meta Meta
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		t.Fatalf("decode meta: %v", err)
	}
	_ = resp.Body.Close()
	if meta.Size != 11 || meta.Complete {
		t.Fatalf("unexpected meta: %#v", meta)
	}

	resp, err = http.Post(ts.URL+"/v1/logs/ee-cd/macbuild-abc/finalize", "application/json", nil)
	if err != nil {
		t.Fatalf("finalize: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("finalize status %d", resp.StatusCode)
	}
	_ = resp.Body.Close()

	resp, err = http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc/meta")
	if err != nil {
		t.Fatalf("get finalized meta: %v", err)
	}
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		t.Fatalf("decode finalized meta: %v", err)
	}
	_ = resp.Body.Close()
	if !meta.Complete || meta.CompletedAt == nil {
		t.Fatalf("unexpected finalized meta: %#v", meta)
	}
}

func TestServerRejectsOffsetConflict(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 0)
	defer ts.Close()
	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "hello").Body.Close()

	resp := putLogChunk(t, ts.URL, "ee-cd", 1, "x")
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("expected conflict, got %d", resp.StatusCode)
	}
}

func TestServerEnforcesBuildLogLimit(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 5)
	defer ts.Close()
	resp := putLogChunk(t, ts.URL, "ee-cd", 0, "123456")
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d", resp.StatusCode)
	}
}

func TestServerRejectsInvalidPath(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 0)
	defer ts.Close()
	resp := putLogChunk(t, ts.URL, "bad_name", 0, "x")
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 for an invalid namespace, got %d", resp.StatusCode)
	}
}

func TestServerFollowStreamsNewBytes(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 0)
	defer ts.Close()

	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "").Body.Close()

	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc?follow=true")
	if err != nil {
		t.Fatalf("follow: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()

	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "first\n").Body.Close()
	buf := make([]byte, len("first\n"))
	if _, err := io.ReadFull(resp.Body, buf); err != nil {
		t.Fatalf("read first chunk: %v", err)
	}
	if !bytes.Equal(buf, []byte("first\n")) {
		t.Fatalf("unexpected first chunk: %q", buf)
	}

	_ = putLogChunk(t, ts.URL, "ee-cd", 6, "second\n").Body.Close()
	resp2, err := http.Post(ts.URL+"/v1/logs/ee-cd/macbuild-abc/finalize", "application/json", nil)
	if err != nil {
		t.Fatalf("finalize: %v", err)
	}
	_ = resp2.Body.Close()

	buf = make([]byte, len("second\n"))
	if _, err := io.ReadFull(resp.Body, buf); err != nil {
		t.Fatalf("read second chunk: %v", err)
	}
	if !bytes.Equal(buf, []byte("second\n")) {
		t.Fatalf("unexpected second chunk: %q", buf)
	}
}

func TestServerMissingLogAndMethodErrors(t *testing.T) {
	t.Parallel()

	ts := newTestServer(t, 0)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/missing/meta")
	if err != nil {
		t.Fatalf("get meta: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 for a missing log, got %d", resp.StatusCode)
	}

	resp, err = http.Post(ts.URL+"/v1/logs/ee-cd/missing/finalize", "application/json", nil)
	if err != nil {
		t.Fatalf("finalize: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 for finalizing a missing log, got %d", resp.StatusCode)
	}

	resp, err = http.Post(ts.URL+"/v1/logs/ee-cd/macbuild-abc", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatalf("post log: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405 for POST, got %d", resp.StatusCode)
	}

	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "hello").Body.Close()
	resp, err = http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc?tail=bad")
	if err != nil {
		t.Fatalf("get bad tail: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 for a bad tail, got %d", resp.StatusCode)
	}

	req, err := http.NewRequest(http.MethodDelete, ts.URL+"/v1/logs/ee-cd/macbuild-abc", nil)
	if err != nil {
		t.Fatalf("delete request: %v", err)
	}
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("delete log: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("expected 204 for delete, got %d", resp.StatusCode)
	}
	resp, err = http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc")
	if err != nil {
		t.Fatalf("get after delete: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 after delete, got %d", resp.StatusCode)
	}
}

func TestServerLimitsConcurrentFollowers(t *testing.T) {
	t.Parallel()

	server := New(Config{
		Store:                NewStore(t.TempDir()),
		MaxConcurrentFollows: 1,
		FollowPollInterval:   10 * time.Millisecond,
	})
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "hello").Body.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(
		ctx, http.MethodGet, ts.URL+"/v1/logs/ee-cd/macbuild-abc?follow=true", nil)
	if err != nil {
		t.Fatalf("follow request: %v", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("start follow: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if _, err := io.ReadFull(resp.Body, make([]byte, len("hello"))); err != nil {
		t.Fatalf("read initial log: %v", err)
	}

	second, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc?follow=true")
	if err != nil {
		t.Fatalf("second follow: %v", err)
	}
	defer func() { _ = second.Body.Close() }()
	if second.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("expected 429 for an extra follower, got %d", second.StatusCode)
	}
}

func TestServerFollowClosesWhenIdle(t *testing.T) {
	t.Parallel()

	server := New(Config{
		Store:              NewStore(t.TempDir()),
		FollowPollInterval: 10 * time.Millisecond,
		FollowIdleTimeout:  80 * time.Millisecond,
	})
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	_ = putLogChunk(t, ts.URL, "ee-cd", 0, "hello").Body.Close()

	resp, err := http.Get(ts.URL + "/v1/logs/ee-cd/macbuild-abc?follow=true")
	if err != nil {
		t.Fatalf("follow: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()

	done := make(chan struct{})
	var body []byte
	go func() {
		body, _ = io.ReadAll(resp.Body)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("follow stream did not close after the idle timeout")
	}
	if string(body) != "hello" {
		t.Fatalf("unexpected log body: %q", body)
	}
}
