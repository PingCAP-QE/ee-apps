package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/google/go-github/v68/github"
)

func TestQueryRegistryImageReturnsFoundResult(t *testing.T) {
	var dispatchCalls int
	var listCalls int
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

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"default_branch": "main",
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/workflows/query-image-tag.yml/dispatches", func(w http.ResponseWriter, r *http.Request) {
		dispatchCalls++

		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode dispatch payload failed: %v", err)
		}

		if payload["ref"] != "main" {
			t.Fatalf("expected dispatch ref main, got %#v", payload["ref"])
		}

		inputs, ok := payload["inputs"].(map[string]any)
		if !ok {
			t.Fatalf("expected dispatch inputs map, got %#v", payload["inputs"])
		}

		if inputs["registry_url"] != "ghcr.io/pingcap/tidb" {
			t.Fatalf("unexpected registry_url: %#v", inputs["registry_url"])
		}
		if inputs["image_tag"] != "nightly" {
			t.Fatalf("unexpected image_tag: %#v", inputs["image_tag"])
		}
		if inputs["credential_ref"] != "query-image-ghcr" {
			t.Fatalf("unexpected credential_ref: %#v", inputs["credential_ref"])
		}

		w.WriteHeader(http.StatusNoContent)
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/workflows/query-image-tag.yml/runs", func(w http.ResponseWriter, r *http.Request) {
		listCalls++

		w.Header().Set("Content-Type", "application/json")
		if dispatchCalls == 0 {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"total_count": 1,
				"workflow_runs": []map[string]any{
					{
						"id":            100,
						"event":         "workflow_dispatch",
						"display_title": "query-image-tag ghcr.io/pingcap/tidb:previous",
						"head_branch":   "main",
						"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/100",
						"created_at":    "2026-05-21T03:00:00Z",
					},
				},
			})
			return
		}

		if listCalls == 2 {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"total_count": 2,
				"workflow_runs": []map[string]any{
					{
						"id":            201,
						"event":         "workflow_dispatch",
						"display_title": "query-image-tag ghcr.io/pingcap/tidb:other",
						"head_branch":   "main",
						"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/201",
						"created_at":    time.Now().UTC().Format(time.RFC3339),
					},
					{
						"id":            100,
						"event":         "workflow_dispatch",
						"display_title": "query-image-tag ghcr.io/pingcap/tidb:previous",
						"head_branch":   "main",
						"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/100",
						"created_at":    "2026-05-21T03:00:00Z",
					},
				},
			})
			return
		}

		_ = json.NewEncoder(w).Encode(map[string]any{
			"total_count": 3,
			"workflow_runs": []map[string]any{
				{
					"id":            201,
					"event":         "workflow_dispatch",
					"display_title": "query-image-tag ghcr.io/pingcap/tidb:other",
					"head_branch":   "main",
					"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/201",
					"created_at":    time.Now().UTC().Format(time.RFC3339),
				},
				{
					"id":            200,
					"event":         "workflow_dispatch",
					"display_title": "query-image-tag ghcr.io/pingcap/tidb:nightly",
					"head_branch":   "main",
					"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/200",
					"created_at":    time.Now().UTC().Format(time.RFC3339),
				},
				{
					"id":            100,
					"event":         "workflow_dispatch",
					"display_title": "query-image-tag ghcr.io/pingcap/tidb:previous",
					"head_branch":   "main",
					"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/100",
					"created_at":    "2026-05-21T03:00:00Z",
				},
			},
		})
	})

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

	resp, err := queryRegistryImage(context.Background(), &registryImageQueryParams{
		Repository: "ghcr.io/pingcap/tidb",
		Tag:        "nightly",
		ImageRef:   "ghcr.io/pingcap/tidb:nightly",
	}, gc, registryImageWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
		CredentialRefs: map[string]string{
			"ghcr.io/pingcap": "query-image-ghcr",
		},
	}, server.Client())
	if err != nil {
		t.Fatalf("queryRegistryImage() error = %v", err)
	}

	if dispatchCalls != 1 {
		t.Fatalf("expected 1 dispatch call, got %d", dispatchCalls)
	}
	if listCalls < 3 {
		t.Fatalf("expected at least 3 list calls, got %d", listCalls)
	}

	for _, fragment := range []string{
		"FOUND",
		"ghcr.io/pingcap/tidb:nightly",
		"Image Created At (OCI)",
		"sha256:abc123",
		"linux/amd64, linux/arm64",
	} {
		if !strings.Contains(resp, fragment) {
			t.Fatalf("expected response to contain %q, got:\n%s", fragment, resp)
		}
	}
}

func TestQueryRegistryImageReturnsRunningWhenWorkflowDoesNotFinishInline(t *testing.T) {
	oldInlineWaitTimeout := registryImageInlineWaitTimeout
	oldRunLookupTick := registryImageWorkflowRunLookupTick
	oldStatusPollTick := registryImageWorkflowStatusPollTick
	registryImageInlineWaitTimeout = 20 * time.Millisecond
	registryImageWorkflowRunLookupTick = 5 * time.Millisecond
	registryImageWorkflowStatusPollTick = 5 * time.Millisecond
	defer func() {
		registryImageInlineWaitTimeout = oldInlineWaitTimeout
		registryImageWorkflowRunLookupTick = oldRunLookupTick
		registryImageWorkflowStatusPollTick = oldStatusPollTick
	}()

	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"default_branch": "main",
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/workflows/query-image-tag.yml/dispatches", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/workflows/query-image-tag.yml/runs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total_count": 1,
			"workflow_runs": []map[string]any{
				{
					"id":            200,
					"event":         "workflow_dispatch",
					"display_title": "query-image-tag ghcr.io/pingcap/tidb:nightly",
					"head_branch":   "main",
					"html_url":      "https://github.com/tidbcloud/docker-image-controller/actions/runs/200",
					"created_at":    time.Now().UTC().Format(time.RFC3339),
				},
			},
		})
	})

	mux.HandleFunc("/repos/tidbcloud/docker-image-controller/actions/runs/200", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":       200,
			"path":     ".github/workflows/query-image-tag.yml",
			"status":   "in_progress",
			"html_url": "https://github.com/tidbcloud/docker-image-controller/actions/runs/200",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	resp, err := queryRegistryImage(context.Background(), &registryImageQueryParams{
		Repository: "ghcr.io/pingcap/tidb",
		Tag:        "nightly",
		ImageRef:   "ghcr.io/pingcap/tidb:nightly",
	}, gc, registryImageWorkflowConfig{
		Owner:    "tidbcloud",
		Repo:     "docker-image-controller",
		Workflow: "query-image-tag.yml",
	}, server.Client())
	if err != nil {
		t.Fatalf("queryRegistryImage() error = %v", err)
	}
	if !strings.Contains(resp, "RUNNING") {
		t.Fatalf("expected response to contain RUNNING, got:\n%s", resp)
	}
}

func TestParseCommandRegistryImageQuery(t *testing.T) {
	params, err := parseCommandRegistryImageQuery([]string{"ghcr.io/pingcap/tidb:nightly"})
	if err != nil {
		t.Fatalf("parseCommandRegistryImageQuery(tagged) error = %v", err)
	}
	if params.Repository != "ghcr.io/pingcap/tidb" || params.Tag != "nightly" {
		t.Fatalf("unexpected tagged params: %+v", params)
	}

	params, err = parseCommandRegistryImageQuery([]string{"ghcr.io/pingcap/tidb", "--tag", "nightly"})
	if err != nil {
		t.Fatalf("parseCommandRegistryImageQuery(--tag) error = %v", err)
	}
	if params.Repository != "ghcr.io/pingcap/tidb" || params.Tag != "nightly" {
		t.Fatalf("unexpected --tag params: %+v", params)
	}
}

func TestResolveRegistryImageCredentialRef(t *testing.T) {
	credentialRef := resolveRegistryImageCredentialRef("https://ghcr.io/pingcap/tidb", map[string]string{
		"ghcr.io":                    "query-image-public",
		"ghcr.io/pingcap":            "query-image-ghcr",
		"ghcr.io/pingcap/tidb-tools": "query-image-tools",
	})
	if credentialRef != "query-image-ghcr" {
		t.Fatalf("expected longest matching credential_ref, got %q", credentialRef)
	}

	credentialRef = resolveRegistryImageCredentialRef("registry.pingcap.net/private/tidb", map[string]string{
		"ghcr.io/pingcap": "query-image-ghcr",
	})
	if credentialRef != "" {
		t.Fatalf("expected empty credential_ref for unmatched registry, got %q", credentialRef)
	}
}

func TestSelectRegistryImageWorkflowRun(t *testing.T) {
	dispatchedAt := time.Date(2026, 5, 21, 4, 0, 0, 0, time.UTC)
	runs := []*github.WorkflowRun{
		{
			ID:           github.Int64(101),
			Event:        github.String("workflow_dispatch"),
			DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:nightly"),
			HeadBranch:   github.String("main"),
			CreatedAt:    &github.Timestamp{Time: dispatchedAt.Add(10 * time.Second)},
		},
		{
			ID:           github.Int64(102),
			Event:        github.String("workflow_dispatch"),
			DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:other"),
			HeadBranch:   github.String("main"),
			CreatedAt:    &github.Timestamp{Time: dispatchedAt.Add(20 * time.Second)},
		},
	}

	run := selectRegistryImageWorkflowRun(runs, "main", "query-image-tag ghcr.io/pingcap/tidb:nightly", 100, dispatchedAt)
	if run == nil {
		t.Fatal("expected a matching run")
	}
	if run.GetID() != 101 {
		t.Fatalf("expected run 101, got %d", run.GetID())
	}
}

func TestSelectRegistryImageWorkflowRunRequiresExactTitle(t *testing.T) {
	dispatchedAt := time.Date(2026, 5, 21, 4, 0, 0, 0, time.UTC)
	runs := []*github.WorkflowRun{
		{
			ID:           github.Int64(101),
			Event:        github.String("workflow_dispatch"),
			DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:other"),
			HeadBranch:   github.String("main"),
			CreatedAt:    &github.Timestamp{Time: dispatchedAt.Add(10 * time.Second)},
		},
	}

	run := selectRegistryImageWorkflowRun(runs, "main", "query-image-tag ghcr.io/pingcap/tidb:nightly", 100, dispatchedAt)
	if run != nil {
		t.Fatalf("expected no run when only fallback candidates are visible, got %d", run.GetID())
	}
}

func TestSelectRegistryImageWorkflowRunPrefersOldestExactMatch(t *testing.T) {
	dispatchedAt := time.Date(2026, 5, 21, 4, 0, 0, 0, time.UTC)
	runs := []*github.WorkflowRun{
		{
			ID:           github.Int64(102),
			Event:        github.String("workflow_dispatch"),
			DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:nightly"),
			HeadBranch:   github.String("main"),
			CreatedAt:    &github.Timestamp{Time: dispatchedAt.Add(20 * time.Second)},
		},
		{
			ID:           github.Int64(101),
			Event:        github.String("workflow_dispatch"),
			DisplayTitle: github.String("query-image-tag ghcr.io/pingcap/tidb:nightly"),
			HeadBranch:   github.String("main"),
			CreatedAt:    &github.Timestamp{Time: dispatchedAt.Add(10 * time.Second)},
		},
	}

	run := selectRegistryImageWorkflowRun(runs, "main", "query-image-tag ghcr.io/pingcap/tidb:nightly", 100, dispatchedAt)
	if run == nil {
		t.Fatal("expected a matching run")
	}
	if run.GetID() != 101 {
		t.Fatalf("expected the oldest exact match to win, got %d", run.GetID())
	}
}
