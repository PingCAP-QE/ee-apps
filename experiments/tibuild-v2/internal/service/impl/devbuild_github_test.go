package impl

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/google/go-github/v69/github"
	"github.com/migueleliasweb/go-github-mock/src/mock"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func refHandler(reference *github.Reference) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(reference)
	}
}

func statusHandler(status int) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		_, _ = io.Copy(w, strings.NewReader(`{"message":"unexpected status"}`))
	}
}

func TestGetGitRefInfo_Branch(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatchHandler(
			mock.GetReposGitRefByOwnerByRepoByRef,
			refHandler(&github.Reference{
				Ref:    github.String("refs/heads/main"),
				Object: &github.GitObject{SHA: github.String("abc123def456")},
			}),
		),
	)

	info, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "branch/main")
	require.NoError(t, err)
	assert.Equal(t, "refs/heads/main", info.Ref)
	assert.Equal(t, "abc123def456", info.SHA)
}

func TestGetGitRefInfo_Tag(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatchHandler(
			mock.GetReposGitRefByOwnerByRepoByRef,
			refHandler(&github.Reference{
				Ref:    github.String("refs/tags/v1.0.0"),
				Object: &github.GitObject{SHA: github.String("tag123sha456")},
			}),
		),
	)

	info, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "tag/v1.0.0")
	require.NoError(t, err)
	assert.Equal(t, "refs/tags/v1.0.0", info.Ref)
	assert.Equal(t, "tag123sha456", info.SHA)
}

func TestGetGitRefInfo_PullRequest(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatch(
			mock.GetReposPullsByOwnerByRepoByPullNumber,
			&github.PullRequest{
				Head: &github.PullRequestBranch{SHA: github.String("pr123head456")},
				Base: &github.PullRequestBranch{Ref: github.String("main")},
			},
		),
	)

	info, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "pull/42")
	require.NoError(t, err)
	assert.Equal(t, "refs/heads/main", info.Ref)
	assert.Equal(t, "main", info.BaseRef)
	assert.Equal(t, "pr123head456", info.SHA)
}

func TestGetGitRefInfo_Commit(t *testing.T) {
	sha := strings.Repeat("a", 40)
	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatch(
			mock.GetReposCommitsByOwnerByRepoByRef,
			&github.RepositoryCommit{SHA: github.String(sha)},
		),
	)

	for _, ref := range []string{"commit/" + sha, sha} {
		info, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", ref)
		require.NoError(t, err, "ref %q", ref)
		assert.Equal(t, sha, info.Ref)
		assert.Equal(t, sha, info.SHA)
	}
}

func TestGetGitRefInfo_NotFound(t *testing.T) {
	tests := []struct {
		name       string
		ref        string
		pattern    mock.EndpointPattern
		wantSubstr string
	}{
		{
			name:       "branch",
			ref:        "branch/nonexistent",
			pattern:    mock.GetReposGitRefByOwnerByRepoByRef,
			wantSubstr: `branch "nonexistent" not found in owner/repo`,
		},
		{
			name:       "tag",
			ref:        "tag/nonexistent",
			pattern:    mock.GetReposGitRefByOwnerByRepoByRef,
			wantSubstr: `tag "nonexistent" not found in owner/repo`,
		},
		{
			name:       "pull",
			ref:        "pull/999",
			pattern:    mock.GetReposPullsByOwnerByRepoByPullNumber,
			wantSubstr: `pull request #999 not found in owner/repo`,
		},
		{
			name:       "commit",
			ref:        "commit/" + strings.Repeat("b", 40),
			pattern:    mock.GetReposCommitsByOwnerByRepoByRef,
			wantSubstr: `commit "` + strings.Repeat("b", 40) + `" not found in owner/repo`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			httpClient := mock.NewMockedHTTPClient(
				mock.WithRequestMatchHandler(tt.pattern, statusHandler(http.StatusNotFound)),
			)

			_, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", tt.ref)
			var notFound *gitRefNotFoundError
			require.ErrorAs(t, err, &notFound)
			assert.Contains(t, notFound.Error(), tt.wantSubstr)
		})
	}
}

func TestGetGitRefInfo_UpstreamFailure(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusTooManyRequests, http.StatusInternalServerError} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			httpClient := mock.NewMockedHTTPClient(
				mock.WithRequestMatchHandler(mock.GetReposGitRefByOwnerByRepoByRef, statusHandler(status)),
			)

			_, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "branch/main")
			var upstream *githubUpstreamError
			require.ErrorAs(t, err, &upstream)
			assert.Equal(t, status, upstream.status)
			assert.Contains(t, upstream.Error(), "owner/repo")
		})
	}
}

func TestGetGitRefInfo_InvalidInput(t *testing.T) {
	t.Run("nil client", func(t *testing.T) {
		_, err := getGitRefInfo(context.Background(), nil, "owner/repo", "branch/main")
		var upstream *githubUpstreamError
		require.ErrorAs(t, err, &upstream)
	})

	t.Run("invalid repo format", func(t *testing.T) {
		httpClient := mock.NewMockedHTTPClient()
		_, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "invalid-repo", "branch/main")
		var notFound *gitRefNotFoundError
		require.ErrorAs(t, err, &notFound)
	})

	t.Run("invalid pull number", func(t *testing.T) {
		httpClient := mock.NewMockedHTTPClient()
		_, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "pull/abc")
		var notFound *gitRefNotFoundError
		require.ErrorAs(t, err, &notFound)
	})

	t.Run("unknown ref format", func(t *testing.T) {
		httpClient := mock.NewMockedHTTPClient()
		_, err := getGitRefInfo(context.Background(), github.NewClient(httpClient), "owner/repo", "unknown/format")
		var notFound *gitRefNotFoundError
		require.ErrorAs(t, err, &notFound)
	})
}

func TestNewGitHubClientWithConfigAPIURL(t *testing.T) {
	client := NewGitHubClientWithConfig(config.Github{APIURL: "http://127.0.0.1:1234"})
	require.NotNil(t, client)
	assert.Equal(t, "http://127.0.0.1:1234/", client.BaseURL.String())

	// An invalid override keeps the default endpoint.
	client = NewGitHubClientWithConfig(config.Github{APIURL: "://bad"})
	assert.Equal(t, "https://api.github.com/", client.BaseURL.String())
}
