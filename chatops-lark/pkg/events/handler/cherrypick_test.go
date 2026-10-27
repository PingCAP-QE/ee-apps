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

type clientScope struct {
	repo        string
	permissions map[string]string
}

type recordingGitHubClientProvider struct {
	scopes []clientScope
	client *github.Client
}

func (p *recordingGitHubClientProvider) newClient(repo string, permissions map[string]string) *github.Client {
	p.scopes = append(p.scopes, clientScope{repo: repo, permissions: permissions})
	return p.client
}

func newCherryPickTestServer(t *testing.T, headRepo string, inviteStatus int) (*httptest.Server, *[]string) {
	t.Helper()

	var invites []string
	mux := http.NewServeMux()
	server := httptest.NewServer(mux)

	mux.HandleFunc("/repos/pingcap/tidb/pulls/42", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"number": 42,
			"head": map[string]any{
				"repo": map[string]any{
					"full_name": headRepo,
					"name":      "tidb",
					"owner":     map[string]any{"login": strings.Split(headRepo, "/")[0]},
				},
			},
		})
	})
	mux.HandleFunc("/repos/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			http.NotFound(w, r)
			return
		}
		invites = append(invites, r.URL.Path)
		w.WriteHeader(inviteStatus)
	})

	t.Cleanup(server.Close)
	return server, &invites
}

func TestCherryPickInviteUsesBrokerScopes(t *testing.T) {
	server, invites := newCherryPickTestServer(t, "ti-chi-bot/tidb", http.StatusCreated)
	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	provider := &recordingGitHubClientProvider{client: gc}
	ctx := context.WithValue(context.Background(), ctxKeyGitHubClient, gitHubClientProvider(provider))

	got, err := cherryPickInvite(ctx, "https://github.com/pingcap/tidb/pull/42", "alice", provider)
	if err != nil {
		t.Fatalf("cherryPickInvite() error = %v", err)
	}
	if !strings.Contains(got, "Successfully invited collaborator alice") {
		t.Fatalf("message = %q", got)
	}

	if len(provider.scopes) != 2 {
		t.Fatalf("client scopes = %#v, want one for the PR and one for the invite", provider.scopes)
	}
	if provider.scopes[0].repo != "pingcap/tidb" {
		t.Fatalf("PR scope = %q, want the base repository", provider.scopes[0].repo)
	}
	if provider.scopes[0].permissions["pull_requests"] != "read" {
		t.Fatalf("PR permissions = %#v, want pull_requests: read", provider.scopes[0].permissions)
	}
	if provider.scopes[1].repo != "ti-chi-bot/tidb" {
		t.Fatalf("invite scope = %q, want the bot fork repository", provider.scopes[1].repo)
	}
	if provider.scopes[1].permissions["administration"] != "write" {
		t.Fatalf("invite permissions = %#v, want administration: write", provider.scopes[1].permissions)
	}
	if len(*invites) != 1 || (*invites)[0] != "/repos/ti-chi-bot/tidb/collaborators/alice" {
		t.Fatalf("invites = %#v", *invites)
	}
}

func TestCherryPickInviteRejectsNonBotFork(t *testing.T) {
	server, invites := newCherryPickTestServer(t, "someone/tidb", http.StatusCreated)
	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	provider := &recordingGitHubClientProvider{client: gc}
	ctx := context.Background()

	if _, err := cherryPickInvite(ctx, "https://github.com/pingcap/tidb/pull/42", "alice", provider); err == nil {
		t.Fatal("expected an error for a PR that is not from ti-chi-bot")
	}
	if len(*invites) != 0 {
		t.Fatalf("invites = %#v, want no invite for a non-bot fork", *invites)
	}
	if len(provider.scopes) != 1 {
		t.Fatalf("client scopes = %#v, want only the PR read", provider.scopes)
	}
}

func TestCherryPickInviteReportsExistingCollaborator(t *testing.T) {
	server, _ := newCherryPickTestServer(t, "ti-chi-bot/tidb", http.StatusNoContent)
	gc := github.NewClient(nil)
	gc.BaseURL, _ = url.Parse(server.URL + "/")

	provider := &recordingGitHubClientProvider{client: gc}
	_, err := cherryPickInvite(context.Background(), "https://github.com/pingcap/tidb/pull/42", "alice", provider)
	if err == nil {
		t.Fatal("expected an error when the user is already a collaborator")
	}
	if !strings.Contains(err.Error(), "already a collaborator") {
		t.Fatalf("error = %v, want the already-a-collaborator message", err)
	}
}

func TestRunCommandCherryPickInviteInvalidURL(t *testing.T) {
	provider := &recordingGitHubClientProvider{client: github.NewClient(nil)}
	ctx := context.WithValue(context.Background(), ctxKeyGitHubClient, gitHubClientProvider(provider))

	if _, err := runCommandCherryPickInvite(ctx, []string{"https://github.com/pingcap/tidb/pull", "alice"}); err == nil {
		t.Fatal("expected an error for a PR URL without a number")
	}
	if len(provider.scopes) != 0 {
		t.Fatalf("client scopes = %#v, want no client for an invalid URL", provider.scopes)
	}
}
