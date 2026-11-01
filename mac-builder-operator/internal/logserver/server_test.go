package logserver

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
