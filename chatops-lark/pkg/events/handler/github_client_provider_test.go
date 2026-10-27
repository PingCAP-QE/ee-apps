package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"testing"

	"github.com/google/go-github/v68/github"
)

type stubGitHubClientProvider struct {
	repo        string
	permissions map[string]string
	client      *github.Client
}

func (s *stubGitHubClientProvider) newClient(repo string, permissions map[string]string) *github.Client {
	s.repo = repo
	s.permissions = permissions
	return s.client
}

func TestRunCommandRepoAdminScopesClientToRequestedRepository(t *testing.T) {
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)
	defer server.Close()

	mux.HandleFunc("/users/testorg", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"login": "testorg", "type": "Organization"})
	})
	mux.HandleFunc("/orgs/testorg/members/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]map[string]any{{"login": "owner1"}})
	})
	mux.HandleFunc("/repos/testorg/testrepo/collaborators", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]map[string]any{{"login": "admin1"}})
	})

	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")
	provider := &stubGitHubClientProvider{client: gc}
	ctx := context.WithValue(context.Background(), ctxKeyGitHubClient, gitHubClientProvider(provider))

	if _, err := runCommandRepoAdmin(ctx, []string{"testorg/testrepo"}); err != nil {
		t.Fatalf("runCommandRepoAdmin() error = %v", err)
	}
	if provider.repo != "testorg/testrepo" {
		t.Fatalf("client repo = %q, want the requested repository", provider.repo)
	}
	if !reflect.DeepEqual(provider.permissions, repoAdminPermissions) {
		t.Fatalf("client permissions = %#v, want %#v", provider.permissions, repoAdminPermissions)
	}
}

func TestRunCommandRepoAdminRejectsMalformedRepository(t *testing.T) {
	provider := &stubGitHubClientProvider{client: github.NewClient(nil)}
	ctx := context.WithValue(context.Background(), ctxKeyGitHubClient, gitHubClientProvider(provider))

	if _, err := runCommandRepoAdmin(ctx, []string{"not-a-repo"}); err == nil {
		t.Fatal("expected an error for a malformed repository argument")
	}
	if provider.repo != "" {
		t.Fatalf("client repo = %q, want no client for a malformed repository", provider.repo)
	}
}

func TestNewRegistryImageGitHubClientScopesToWorkflowRepository(t *testing.T) {
	provider := &stubGitHubClientProvider{client: github.NewClient(nil)}
	ctx := context.WithValue(context.Background(), ctxKeyGitHubClient, gitHubClientProvider(provider))

	gc, downloadClient, err := newRegistryImageGitHubClient(ctx, registryImageWorkflowConfig{
		Owner: "tidbcloud",
		Repo:  "docker-image-controller",
	})
	if err != nil {
		t.Fatalf("newRegistryImageGitHubClient() error = %v", err)
	}
	if gc == nil {
		t.Fatal("github client is nil")
	}
	if provider.repo != "tidbcloud/docker-image-controller" {
		t.Fatalf("client repo = %q", provider.repo)
	}
	if !reflect.DeepEqual(provider.permissions, registryImagePermissions) {
		t.Fatalf("client permissions = %#v, want %#v", provider.permissions, registryImagePermissions)
	}
	if downloadClient == nil {
		t.Fatal("download client is nil")
	}
	if downloadClient.Transport != nil {
		t.Fatal("the artifact download client must not carry the GitHub API auth transport")
	}
}
