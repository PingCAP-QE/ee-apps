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

	mux.HandleFunc("/users/testorg", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"login": "testorg",
			"type":  "Organization",
		})
	})

	mux.HandleFunc("/repos/testorg/testrepo/collaborators", func(w http.ResponseWriter, r *http.Request) {
		affiliation := r.URL.Query().Get("affiliation")
		permission := r.URL.Query().Get("permission")

		if affiliation != "direct" || permission != "admin" {
			t.Errorf("Expected affiliation=direct and permission=admin, got affiliation=%s, permission=%s", affiliation, permission)
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)

		collaborators := []map[string]interface{}{
			{"login": "owner1", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "owner2", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "owner3", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "admin1", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "admin2", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "admin3", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "owner4", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "owner5", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
			{"login": "owner6", "permissions": map[string]bool{"admin": true, "push": true, "pull": true}},
		}
		json.NewEncoder(w).Encode(collaborators)
	})

	mux.HandleFunc("/orgs/testorg/members/", func(w http.ResponseWriter, r *http.Request) {
		role := r.URL.Query().Get("role")
		if role != "admin" {
			t.Errorf("Expected role=admin, got role=%s", role)
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)

		orgOwners := []map[string]interface{}{
			{"login": "owner1", "type": "User"},
			{"login": "owner2", "type": "User"},
			{"login": "owner3", "type": "User"},
			{"login": "owner4", "type": "User"},
			{"login": "owner5", "type": "User"},
			{"login": "owner6", "type": "User"},
		}
		json.NewEncoder(w).Encode(orgOwners)
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "testorg", "testrepo")

	if err != nil {
		t.Fatalf("getOrgAdmins() error = %v", err)
	}

	expectedAdmins := []string{"admin1", "admin2", "admin3"}

	for _, expectedAdmin := range expectedAdmins {
		if !strings.Contains(result, "@"+expectedAdmin) {
			t.Errorf("getOrgAdmins() result should contain %q, got:\n%s", expectedAdmin, result)
		}
	}

	excludedOwners := []string{"owner1", "owner2", "owner3", "owner4", "owner5", "owner6"}
	for _, owner := range excludedOwners {
		if strings.Contains(result, "@"+owner) {
			t.Errorf("getOrgAdmins() result should NOT contain org owner %q, got:\n%s", owner, result)
		}
	}

	if !strings.Contains(result, "Repository administrators for `testorg/testrepo`:") {
		t.Errorf("getOrgAdmins() result should contain header, got:\n%s", result)
	}

	if !strings.Contains(result, "→ Contact any contact whose GitHub ID is in the above list") {
		t.Errorf("getOrgAdmins() result should contain footer, got:\n%s", result)
	}

	t.Logf("Test passed! Result:\n%s", result)
}

func TestGetOrgAdmins_PersonalRepo(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/users/testuser", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"login": "testuser",
			"type":  "User",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := queryRepoAdmins(ctx, "testuser/myrepo", gc)

	if err != nil {
		t.Fatalf("queryRepoAdmins() error = %v", err)
	}

	if !strings.Contains(result, "Repository administrator for `testuser/myrepo`:") {
		t.Errorf("queryRepoAdmins() should return personal repo format, got:\n%s", result)
	}

	if !strings.Contains(result, "1. testuser") {
		t.Errorf("queryRepoAdmins() should contain owner name, got:\n%s", result)
	}

	t.Logf("Test passed! Result:\n%s", result)
}

func TestGetOrgAdmins_NoAdmins(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/repos/testorg/emptyrepo/collaborators", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode([]interface{}{})
	})

	mux.HandleFunc("/orgs/testorg/members", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode([]interface{}{})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "testorg", "emptyrepo")

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
	mux.HandleFunc("/repos/testorg/notfound/collaborators", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"message": "Not Found",
		})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	ctx := context.Background()
	result, err := getOrgAdmins(ctx, gc, "testorg", "notfound")

	if err == nil {
		t.Errorf("getOrgAdmins() should return error for 404, got result: %s", result)
	}

	if !strings.Contains(err.Error(), "repository not found or no access permission") {
		t.Errorf("getOrgAdmins() error message should mention not found, got: %v", err)
	}
}
