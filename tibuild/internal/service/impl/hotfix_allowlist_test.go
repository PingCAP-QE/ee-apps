package impl

import (
	"context"
	"net/http"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func newHotfixForAllowlist(t *testing.T, entries []string) hotfix.Service {
	t.Helper()
	logger := zerolog.Nop()
	return NewHotfix(&logger, NewGitHubClient("test-token"), &config.Service{Hotfix: config.Hotfix{
		GithubRepoAllowlist: entries,
	}})
}

// Creating a tag writes into a repository consumers release from, and the
// repository is whatever the caller typed, so an unset or non-matching list has
// to refuse before any GitHub call happens.
func TestBumpTagForTidbxRequiresAllowlistedRepository(t *testing.T) {
	branch := "release-8.5"
	payload := func(repo string) *hotfix.BumpTagForTidbxPayload {
		return &hotfix.BumpTagForTidbxPayload{Repo: repo, Branch: &branch, Author: "someone@pingcap.com"}
	}

	tests := []struct {
		name     string
		entries  []string
		repo     string
		expected int
	}{
		{"an empty allowlist denies everything", nil, "pingcap/tidb", http.StatusForbidden},
		{"another repository is denied", []string{"pingcap/tidb"}, "evil/fork", http.StatusForbidden},
		{"the owner cannot be widened by a sub-repository", []string{"pingcap/tidb"}, "pingcap/tidb/extra", http.StatusForbidden},
		{"an allowlisted repository passes the gate", []string{"pingcap/tidb"}, "pingcap/tidb", 0},
		{"an owner wildcard passes the gate", []string{"pingcap/*"}, "pingcap/tiflash", 0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			svc := newHotfixForAllowlist(t, tc.entries)
			_, err := svc.BumpTagForTidbx(context.Background(), payload(tc.repo))

			if tc.expected == 0 {
				// The gate passed; with a nil GitHub client the next step fails,
				// but not with the allowlist error.
				if err != nil {
					require.NotContains(t, err.Error(), "not allowed")
				}
				return
			}
			require.Error(t, err)
			var httpErr *hotfix.HTTPErrorForbidden
			require.ErrorAs(t, err, &httpErr)
			require.Equal(t, tc.expected, httpErr.Code)
			require.Contains(t, httpErr.Message, "not allowed")
		})
	}
}

// A reload that drops the allowlist must close the endpoint, not open it.
func TestHotfixReloadAppliesAllowlist(t *testing.T) {
	svc := newHotfixForAllowlist(t, []string{"pingcap/tidb"})
	reloader, ok := svc.(interface {
		Reload(*config.Service)
	})
	require.True(t, ok, "hotfix service must accept configuration reloads")

	branch := "release-8.5"
	_, err := svc.BumpTagForTidbx(context.Background(), &hotfix.BumpTagForTidbxPayload{
		Repo: "pingcap/tidb", Branch: &branch, Author: "someone@pingcap.com",
	})
	if err != nil {
		require.NotContains(t, err.Error(), "not allowed", "allowlisted repo must pass the gate before the reload")
	}

	reloader.Reload(&config.Service{})
	_, err = svc.BumpTagForTidbx(context.Background(), &hotfix.BumpTagForTidbxPayload{
		Repo: "pingcap/tidb", Branch: &branch, Author: "someone@pingcap.com",
	})
	require.Error(t, err)
	var httpErr *hotfix.HTTPErrorForbidden
	require.ErrorAs(t, err, &httpErr)
	require.Contains(t, httpErr.Message, "not allowed")
}
