package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/google/go-github/v68/github"
)

func TestGetOrgAdmins_ExcludeOrgOwners(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/users/pingcap", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"login": "pingcap",
			"type":  "Organization",
		})
	})

	mux.HandleFunc("/repos/pingcap/tidb/collaborators", func(w http.ResponseWriter, r *http.Request) {
		affiliation := r.URL.Query().Get("affiliation")
		permission := r.URL.Query().Get("permission")

		if affiliation != "all" || permission != "admin" {
			t.Errorf("Expected affiliation=all and permission=admin, got affiliation=%s, permission=%s", affiliation, permission)
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)

		collaborators := []map[string]interface{}{
			{"login": "c4pt0r", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "ngaut", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "siddontang", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "bb7133", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "qiuyesuifeng", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "hawkingrei", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "iamxy", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "zhangjinpeng87", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "ti-chi-bot", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
		}
		json.NewEncoder(w).Encode(collaborators)
	})

	orgOwners := map[string]bool{
		"c4pt0r":       true,
		"iamxy":        true,
		"ngaut":        true,
		"qiuyesuifeng": true,
		"siddontang":   true,
		"ti-chi-bot":   true,
	}

	mux.HandleFunc("/orgs/pingcap/memberships/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(r.URL.Path, "/")
		username := parts[len(parts)-1]

		w.Header().Set("Content-Type", "application/json")

		if orgOwners[username] {
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"role":  "admin",
				"state": "active",
			})
		} else {
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"role":  "member",
				"state": "active",
			})
		}
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "pingcap", "tidb")

	if err != nil {
		t.Fatalf("getOrgAdmins() error = %v", err)
	}

	expectedAdmins := []string{"bb7133", "hawkingrei", "zhangjinpeng87"}

	for _, expectedAdmin := range expectedAdmins {
		if !strings.Contains(result, expectedAdmin) {
			t.Errorf("getOrgAdmins() result should contain %q, got:\n%s", expectedAdmin, result)
		}
	}

	excludedOwners := []string{"c4pt0r", "iamxy", "ngaut", "qiuyesuifeng", "siddontang", "ti-chi-bot"}
	for _, owner := range excludedOwners {
		if strings.Contains(result, "@"+owner) {
			t.Errorf("getOrgAdmins() result should NOT contain org owner %q, got:\n%s", owner, result)
		}
	}

	if !strings.Contains(result, "Repository administrators for `pingcap/tidb`:") {
		t.Errorf("getOrgAdmins() result should contain header, got:\n%s", result)
	}

	if !strings.Contains(result, "→ Contact any admin above for write access") {
		t.Errorf("getOrgAdmins() result should contain footer, got:\n%s", result)
	}

	t.Logf("Test passed! Result:\n%s", result)
}

func TestGetOrgAdmins_PersonalRepo(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/users/octocat", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"login": "octocat",
			"type":  "User",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := queryRepoAdmins(ctx, "octocat/Hello-World", gc)

	if err != nil {
		t.Fatalf("queryRepoAdmins() error = %v", err)
	}

	if !strings.Contains(result, "Repository administrator for `octocat/Hello-World`:") {
		t.Errorf("queryRepoAdmins() should return personal repo format, got:\n%s", result)
	}

	if !strings.Contains(result, "1. octocat") {
		t.Errorf("queryRepoAdmins() should contain owner name, got:\n%s", result)
	}

	t.Logf("Test passed! Result:\n%s", result)
}

func TestGetOrgAdmins_NoAdmins(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/test/repo/collaborators", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode([]interface{}{})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "test", "repo")

	if err != nil {
		t.Fatalf("getOrgAdmins() error = %v", err)
	}

	if !strings.Contains(result, "No repository administrators found") {
		t.Errorf("getOrgAdmins() should return no admins message, got:\n%s", result)
	}

	if !strings.Contains(result, "→ Contact repository owner for write access") {
		t.Errorf("getOrgAdmins() should contain contact message, got:\n%s", result)
	}

	t.Logf("Test passed! Result:\n%s", result)
}

func TestGetOrgAdmins_RepoNotFound(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	// Mock GET /repos/test/notfound/collaborators - return 404
	mux.HandleFunc("/repos/test/notfound/collaborators", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"message": "Not Found",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "test", "notfound")

	if err == nil {
		t.Errorf("getOrgAdmins() should return error for 404, got result: %s", result)
	}

	if !strings.Contains(err.Error(), "repository not found or no access permission") {
		t.Errorf("getOrgAdmins() error message should mention not found, got: %v", err)
	}
}
