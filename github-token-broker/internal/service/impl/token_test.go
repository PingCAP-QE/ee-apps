package impl

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	goa "goa.design/goa/v3/pkg"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authn"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/cache"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/githubapp"
	tokenapi "github.com/PingCAP-QE/ee-apps/github-token-broker/internal/service/gen/token"
)

type fakeMinter struct {
	token githubapp.Token
	err   error
	calls int
	repos []string
	perms map[string]string
}

func (f *fakeMinter) Mint(_ context.Context, _, _ string, repositories []string, permissions map[string]string) (githubapp.Token, error) {
	f.calls++
	f.repos = repositories
	f.perms = permissions
	return f.token, f.err
}

func testConfig(t *testing.T, contentsLevel, fallbackPAT string) *config.Config {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	fallback := ""
	if fallbackPAT != "" {
		fallback = fmt.Sprintf("  fallback_pat: %s\n", fallbackPAT)
	}
	content := fmt.Sprintf(`
github:
  app_id: 1
  private_key_path: /tmp/key.pem
%s
cache:
  ttl_margin: 1m
  max_entries: 10
policy:
  rules:
    - caller: {namespace: ci, service_account: checkout}
      allow:
        - owner: example-org
          repositories: ["example-repo", "another-repo"]
          max_permissions: {contents: %s}
`, fallback, contentsLevel)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func newService(cfg *config.Config, minter githubapp.Minter, readiness func(context.Context) error) *Token {
	return NewToken(TokenOptions{
		Config:    config.StaticProvider{Cfg: cfg},
		Minter:    minter,
		Cache:     cache.New(time.Minute, 10),
		Readiness: readiness,
		Now:       func() time.Time { return time.Unix(1000, 0) },
	})
}

func callerContext() context.Context {
	return authn.WithCaller(context.Background(), authz.Caller{Namespace: "ci", ServiceAccount: "checkout"})
}

func serviceError(t *testing.T, err error) *goa.ServiceError {
	t.Helper()
	var gerr *goa.ServiceError
	if !errors.As(err, &gerr) {
		t.Fatalf("expected a goa.ServiceError, got %v", err)
	}
	return gerr
}

func TestIssueApp(t *testing.T) {
	minter := &fakeMinter{token: githubapp.Token{
		Value:          "ghs_test",
		ExpiresAt:      time.Unix(2000, 0),
		InstallationID: 42,
	}}
	svc := newService(testConfig(t, "read", ""), minter, nil)

	result, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Token != "ghs_test" || result.Source != "app" {
		t.Fatalf("unexpected result: %#v", result)
	}
	if result.ExpiresAt == nil || *result.ExpiresAt != "1970-01-01T00:33:20Z" {
		t.Fatalf("unexpected expiry: %#v", result.ExpiresAt)
	}
	if result.InstallationID == nil || *result.InstallationID != 42 {
		t.Fatalf("unexpected installation id: %#v", result.InstallationID)
	}
	if len(minter.repos) != 0 {
		t.Fatalf("expected an installation-wide read token, got %#v", minter.repos)
	}
	if minter.perms["contents"] != "read" {
		t.Fatalf("unexpected permissions: %#v", minter.perms)
	}
}

func TestIssueWriteNarrowsRepository(t *testing.T) {
	minter := &fakeMinter{token: githubapp.Token{Value: "ghs_write", ExpiresAt: time.Unix(2000, 0), InstallationID: 42}}
	svc := newService(testConfig(t, "write", ""), minter, nil)

	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{
		Repo:        "example-org/example-repo",
		Permissions: map[string]string{"contents": "write"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(minter.repos) != 1 || minter.repos[0] != "example-repo" {
		t.Fatalf("write token must be narrowed, got %#v", minter.repos)
	}
}

func TestIssueForbidden(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/not-allowed"})
	if gerr := serviceError(t, err); gerr.Name != "forbidden" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueBadRequest(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "invalid"})
	if gerr := serviceError(t, err); gerr.Name != "bad_request" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueUnauthorized(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, nil)
	_, err := svc.Issue(context.Background(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if gerr := serviceError(t, err); gerr.Name != "unauthorized" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueFallbackPAT(t *testing.T) {
	minter := &fakeMinter{err: githubapp.ErrNoInstallation}
	svc := newService(testConfig(t, "read", "pat-token"), minter, nil)

	result, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Token != "pat-token" || result.Source != "pat" {
		t.Fatalf("unexpected result: %#v", result)
	}
	if result.ExpiresAt == nil {
		t.Fatal("expected an expiry for the PAT fallback")
	}
}

func TestIssueNoInstallation(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{err: githubapp.ErrNoInstallation}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if gerr := serviceError(t, err); gerr.Name != "not_found" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueUpstream(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{err: errors.New("boom")}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if gerr := serviceError(t, err); gerr.Name != "upstream" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueBadNarrowing(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{
		Repo:         "example-org/example-repo",
		Repositories: []string{"another-repo"},
	})
	if gerr := serviceError(t, err); gerr.Name != "bad_request" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueTimeout(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{err: context.DeadlineExceeded}, nil)
	_, err := svc.Issue(callerContext(), &tokenapi.TokenPayload{Repo: "example-org/example-repo"})
	if gerr := serviceError(t, err); gerr.Name != "timeout" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestIssueCacheHit(t *testing.T) {
	minter := &fakeMinter{token: githubapp.Token{Value: "ghs_cached", ExpiresAt: time.Now().Add(time.Hour), InstallationID: 42}}
	svc := newService(testConfig(t, "read", ""), minter, nil)
	payload := &tokenapi.TokenPayload{Repo: "example-org/example-repo"}
	for i := 0; i < 2; i++ {
		if _, err := svc.Issue(callerContext(), payload); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if minter.calls != 1 {
		t.Fatalf("expected one mint, got %d", minter.calls)
	}
}

func TestHealth(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, nil)
	if value, err := svc.Healthz(context.Background()); err != nil || value != "ok" {
		t.Fatalf("unexpected healthz result: %q %v", value, err)
	}
	if value, err := svc.Readyz(context.Background()); err != nil || value != "ready" {
		t.Fatalf("unexpected readyz result: %q %v", value, err)
	}
}

func TestReadyzFailure(t *testing.T) {
	svc := newService(testConfig(t, "read", ""), &fakeMinter{}, func(context.Context) error {
		return errors.New("kubernetes unavailable")
	})
	_, err := svc.Readyz(context.Background())
	if gerr := serviceError(t, err); gerr.Name != "not_ready" {
		t.Fatalf("unexpected error name: %s", gerr.Name)
	}
}

func TestNormalizeRepositories(t *testing.T) {
	got := normalizeRepositories([]string{" b ", "a", "B", "a", ""})
	if len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("unexpected repositories: %#v", got)
	}
}
