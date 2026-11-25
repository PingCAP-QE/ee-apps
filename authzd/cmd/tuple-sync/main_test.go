package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

type fakeOpenFGA struct {
	server         *httptest.Server
	writes         int
	deletes        int
	lastWriteBody  map[string]any
	lastDeleteBody map[string]any
}

func newFakeOpenFGA(t *testing.T, existing string) *fakeOpenFGA {
	t.Helper()
	store := &fakeOpenFGA{}
	store.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/read"):
			_, _ = w.Write([]byte(`{"tuples":` + existing + `}`))
		case strings.HasSuffix(r.URL.Path, "/write"):
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode write body: %v", err)
			}
			if _, ok := body["writes"]; ok {
				store.writes++
				store.lastWriteBody = body
			}
			if _, ok := body["deletes"]; ok {
				store.deletes++
				store.lastDeleteBody = body
			}
			_, _ = w.Write([]byte(`{}`))
		default:
			t.Errorf("unexpected OpenFGA request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(store.server.Close)
	return store
}

func snapshotFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "grants.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write snapshot: %v", err)
	}
	return path
}

func env(serverURL string) func(string) string {
	return func(key string) string {
		switch key {
		case "AUTHZD_OPENFGA_URL":
			return serverURL
		case "AUTHZD_OPENFGA_STORE_ID", "AUTHZD_OPENFGA_MODEL_ID":
			return testULID
		default:
			return ""
		}
	}
}

const reviewedSnapshot = `{
  "source": "reviewed-grants",
  "revision": "git:9dc1cce",
  "generated_at": "2026-11-25T09:00:00Z",
  "complete": true,
  "tuples": [
    {"user": "team:release#member", "relation": "caller", "object": "capability:tibuild.devbuild.rerun"}
  ]
}`

func TestRunAppliesReviewedSnapshot(t *testing.T) {
	fga := newFakeOpenFGA(t, `[]`)
	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--snapshot", snapshotFile(t, reviewedSnapshot),
		"--object-type", "capability", "--relation", "caller",
	}, env(fga.server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("run() = %d, stderr = %s", code, stderr.String())
	}
	if fga.writes != 1 {
		t.Fatalf("write requests = %d, want 1", fga.writes)
	}
	if !strings.Contains(stdout.String(), `authzd_tuple_sync_total{outcome="applied"} 1`) {
		t.Fatalf("stdout metrics = %s", stdout.String())
	}
	if !strings.Contains(stderr.String(), `"outcome":"applied"`) {
		t.Fatalf("stderr log = %s", stderr.String())
	}
}

func TestRunDryRunDoesNotWrite(t *testing.T) {
	fga := newFakeOpenFGA(t, `[]`)
	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--snapshot", snapshotFile(t, reviewedSnapshot),
		"--object-type", "capability", "--relation", "caller", "--dry-run",
	}, env(fga.server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("run() = %d, stderr = %s", code, stderr.String())
	}
	if fga.writes != 0 || fga.deletes != 0 {
		t.Fatalf("dry run wrote tuples: %+v", fga)
	}
	if !strings.Contains(stdout.String(), `authzd_tuple_sync_total{outcome="planned"} 1`) {
		t.Fatalf("stdout metrics = %s", stdout.String())
	}
}

func TestRunFailsClosedOnInvalidInput(t *testing.T) {
	fga := newFakeOpenFGA(t, `[]`)
	incomplete := strings.Replace(reviewedSnapshot, `"complete": true`, `"complete": false`, 1)
	cases := map[string]struct {
		args   []string
		getenv func(string) string
	}{
		"missing snapshot flag": {
			args:   []string{"--object-type", "capability", "--relation", "caller"},
			getenv: env(fga.server.URL),
		},
		"missing scope": {
			args:   []string{"--snapshot", snapshotFile(t, reviewedSnapshot)},
			getenv: env(fga.server.URL),
		},
		"incomplete snapshot": {
			args: []string{
				"--snapshot", snapshotFile(t, incomplete),
				"--object-type", "capability", "--relation", "caller",
			},
			getenv: env(fga.server.URL),
		},
		"unconfigured openfga": {
			args: []string{
				"--snapshot", snapshotFile(t, reviewedSnapshot),
				"--object-type", "capability", "--relation", "caller",
			},
			getenv: func(string) string { return "" },
		},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := run(testCase.args, testCase.getenv, &stdout, &stderr); code == 0 {
				t.Fatalf("run() succeeded, stderr = %s", stderr.String())
			}
			if fga.writes != 0 {
				t.Fatalf("invalid input wrote tuples: %+v", fga)
			}
		})
	}
}

func TestRunRevokesRemovedGrants(t *testing.T) {
	fga := newFakeOpenFGA(t, `[{"key":{"user":"team:gone#member","relation":"caller","object":"capability:tibuild.devbuild.rerun"},"timestamp":"2026-11-20T09:00:00Z"}]`)
	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--snapshot", snapshotFile(t, reviewedSnapshot),
		"--object-type", "capability", "--relation", "caller",
	}, env(fga.server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("run() = %d, stderr = %s", code, stderr.String())
	}
	if fga.deletes != 1 || fga.writes != 1 {
		t.Fatalf("write/delete requests = %d/%d, want 1/1", fga.writes, fga.deletes)
	}
	deletes, _ := fga.lastDeleteBody["deletes"].(map[string]any)
	keys, _ := deletes["tuple_keys"].([]any)
	if len(keys) != 1 {
		t.Fatalf("deletes = %v", deletes)
	}
	first, _ := keys[0].(map[string]any)
	if first["user"] != "team:gone#member" {
		t.Fatalf("deleted %v, want the revoked grant", first)
	}
}
