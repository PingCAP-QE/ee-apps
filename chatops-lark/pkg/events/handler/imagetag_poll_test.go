package handler

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/go-github/v68/github"
)

func TestPollImageTagWorkflowSuccess(t *testing.T) {
	archiveBytes := buildImageTagArtifactArchive(t, `{
  "image_ref": "ghcr.io/pingcap/tidb:nightly",
  "created_at": "2026-05-21T04:00:00Z",
  "digest": "sha256:abc123",
  "multi_arch": true,
  "platforms": ["linux/amd64", "linux/arm64"],
  "labels": {"org.opencontainers.image.revision": "deadbeef"}
}`)

	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/200", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":         200,
			"path":       ".github/workflows/query-image-tag.yml",
			"status":     "completed",
			"conclusion": "success",
			"html_url":   "https://github.com/tidbcloud/docker-image-controller/actions/runs/200",
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/200/artifacts", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total_count": 1,
			"artifacts": []map[string]any{
				{
					"id":      456,
					"name":    "result.json",
					"expired": false,
				},
			},
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/artifacts/456/zip", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, server.URL+"/download/result.zip", http.StatusFound)
	})

	mux.HandleFunc("/download/result.zip", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/zip")
		_, _ = w.Write(archiveBytes)
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	resp, err := pollImageTagWorkflow(context.Background(), gc, imageTagWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, 200, server.Client())
	if err != nil {
		t.Fatalf("pollImageTagWorkflow() error = %v", err)
	}

	for _, fragment := range []string{
		"ghcr.io/pingcap/tidb:nightly",
		"sha256:abc123",
		"linux/amd64, linux/arm64",
		"org.opencontainers.image.revision",
	} {
		if !strings.Contains(resp, fragment) {
			t.Fatalf("expected response to contain %q, got:\n%s", fragment, resp)
		}
	}
}

func TestPollImageTagWorkflowFailure(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/201", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":         201,
			"path":       ".github/workflows/query-image-tag.yml",
			"status":     "completed",
			"conclusion": "failure",
			"html_url":   "https://github.com/tidbcloud/docker-image-controller/actions/runs/201",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	_, err := pollImageTagWorkflow(context.Background(), gc, imageTagWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, 201, server.Client())
	if err == nil {
		t.Fatal("expected pollImageTagWorkflow() to fail")
	}
	if !strings.Contains(err.Error(), "conclusion") {
		t.Fatalf("expected failure to mention conclusion, got: %v", err)
	}
}

func TestPollImageTagWorkflowRejectsMismatchedWorkflow(t *testing.T) {
	var artifactListed atomic.Bool

	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/202", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":           202,
			"path":         ".github/workflows/release.yml",
			"workflow_id":  999,
			"workflow_url": server.URL + "/repos/tidbcloud/docker-image-controller/actions/workflows/999",
			"status":       "completed",
			"conclusion":   "success",
			"html_url":     "https://github.com/tidbcloud/docker-image-controller/actions/runs/202",
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/workflows/query-image-tag.yml", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":   123,
			"path": ".github/workflows/query-image-tag.yml",
			"url":  server.URL + "/repos/tidbcloud/docker-image-controller/actions/workflows/123",
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/202/artifacts", func(w http.ResponseWriter, r *http.Request) {
		artifactListed.Store(true)
		http.Error(w, "artifacts should not be queried for mismatched workflows", http.StatusInternalServerError)
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	_, err := pollImageTagWorkflow(context.Background(), gc, imageTagWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, 202, server.Client())
	if err == nil {
		t.Fatal("expected pollImageTagWorkflow() to reject mismatched workflow")
	}
	if !strings.Contains(err.Error(), "does not belong") {
		t.Fatalf("expected mismatched workflow error, got: %v", err)
	}
	if artifactListed.Load() {
		t.Fatal("expected artifact lookup to be skipped for mismatched workflow")
	}
}

func TestExtractResultJSONFromArtifact(t *testing.T) {
	archiveBytes := buildImageTagArtifactArchive(t, `{"image_ref":"ghcr.io/pingcap/tidb:nightly"}`)

	body, err := extractResultJSONFromArtifact(archiveBytes, "result.json")
	if err != nil {
		t.Fatalf("extractResultJSONFromArtifact() error = %v", err)
	}
	if string(body) != `{"image_ref":"ghcr.io/pingcap/tidb:nightly"}` {
		t.Fatalf("unexpected extracted body: %s", string(body))
	}
}

func buildImageTagArtifactArchive(t *testing.T, resultJSON string) []byte {
	t.Helper()

	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	fw, err := zw.Create("result.json")
	if err != nil {
		t.Fatalf("create zip entry failed: %v", err)
	}
	if _, err := fw.Write([]byte(resultJSON)); err != nil {
		t.Fatalf("write zip entry failed: %v", err)
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("close zip writer failed: %v", err)
	}

	return buf.Bytes()
}
