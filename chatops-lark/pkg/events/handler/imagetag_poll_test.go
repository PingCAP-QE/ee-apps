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
	"testing"

	"github.com/google/go-github/v68/github"
)

func TestBuildRegistryImageQueryOutcomeNotFound(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/201/jobs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total_count": 1,
			"jobs": []map[string]any{
				{
					"id":         301,
					"name":       "Inspect image metadata",
					"conclusion": "failure",
					"steps": []map[string]any{
						{
							"name":       "Inspect image and write result artifact",
							"conclusion": "failure",
						},
					},
				},
			},
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/jobs/301/logs", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, server.URL+"/download/not-found.log", http.StatusFound)
	})

	mux.HandleFunc("/download/not-found.log", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("not found: ghcr.io/pingcap/tidb:nightly"))
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	outcome, err := buildRegistryImageQueryOutcome(context.Background(), gc, registryImageWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, &github.WorkflowRun{
		ID:           github.Int64(201),
		Path:         github.String(".github/workflows/query-image-tag.yml"),
		Status:       github.String("completed"),
		Conclusion:   github.String("failure"),
		DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:nightly"),
		HTMLURL:      github.String("https://github.com/tidbcloud/docker-image-controller/actions/runs/201"),
	}, server.Client())
	if err != nil {
		t.Fatalf("buildRegistryImageQueryOutcome() error = %v", err)
	}
	if outcome.Status != registryImageQueryStatusNotFound {
		t.Fatalf("expected NOT_FOUND outcome, got %s", outcome.Status)
	}

	resp, err := renderRegistryImageQueryResponse(outcome)
	if err != nil {
		t.Fatalf("renderRegistryImageQueryResponse() error = %v", err)
	}
	if !strings.Contains(resp, "NOT_FOUND") {
		t.Fatalf("expected rendered response to contain NOT_FOUND, got:\n%s", resp)
	}
}

func TestBuildRegistryImageQueryOutcomeAuthFailed(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/202/jobs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total_count": 1,
			"jobs": []map[string]any{
				{
					"id":         302,
					"name":       "Inspect image metadata",
					"conclusion": "failure",
					"steps": []map[string]any{
						{
							"name":       "Authenticate registry",
							"conclusion": "failure",
						},
					},
				},
			},
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	outcome, err := buildRegistryImageQueryOutcome(context.Background(), gc, registryImageWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, &github.WorkflowRun{
		ID:           github.Int64(202),
		Path:         github.String(".github/workflows/query-image-tag.yml"),
		Status:       github.String("completed"),
		Conclusion:   github.String("failure"),
		DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:nightly"),
		HTMLURL:      github.String("https://github.com/tidbcloud/docker-image-controller/actions/runs/202"),
	}, server.Client())
	if err != nil {
		t.Fatalf("buildRegistryImageQueryOutcome() error = %v", err)
	}
	if outcome.Status != registryImageQueryStatusAuthFailed {
		t.Fatalf("expected AUTH_FAILED outcome, got %s", outcome.Status)
	}
}

func TestWaitForRegistryImageWorkflowCompletionRejectsMismatchedWorkflow(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/203", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":           203,
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

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	_, err := waitForRegistryImageWorkflowCompletion(context.Background(), gc, registryImageWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, 203)
	if err == nil {
		t.Fatal("expected waitForRegistryImageWorkflowCompletion() to reject mismatched workflow")
	}
	if !strings.Contains(err.Error(), "does not belong") {
		t.Fatalf("expected mismatched workflow error, got: %v", err)
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
