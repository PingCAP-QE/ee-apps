package impl

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/google/go-github/v69/github"
	"github.com/migueleliasweb/go-github-mock/src/mock"
)

func TestGetGhRefSha_Branch(t *testing.T) {
	fullRepo := "owner/repo"
	branchName := "main"
	expectedSHA := "abc123def456"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatch(
			mock.GetReposBranchesByOwnerByRepoByBranch,
			&github.Branch{
				Name: github.Ptr(branchName),
				Commit: &github.RepositoryCommit{
					SHA: github.Ptr(expectedSHA),
				},
			},
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "branch/"+branchName)
	if sha != expectedSHA {
		t.Fatalf("expected sha %s, got %s", expectedSHA, sha)
	}
}

func TestGetGhRefSha_Tag(t *testing.T) {
	fullRepo := "owner/repo"
	tagName := "v1.0.0"
	expectedSHA := "tag123sha456"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatch(
			mock.GetReposGitRefByOwnerByRepoByRef,
			&github.Reference{
				Ref: github.Ptr("refs/tags/" + tagName),
				Object: &github.GitObject{
					SHA: github.Ptr(expectedSHA),
				},
			},
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "tag/"+tagName)
	if sha != expectedSHA {
		t.Fatalf("expected sha %s, got %s", expectedSHA, sha)
	}
}

func TestGetGhRefSha_PullRequest(t *testing.T) {
	fullRepo := "owner/repo"
	expectedSHA := "pr123head456"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatch(
			mock.GetReposPullsByOwnerByRepoByPullNumber,
			&github.PullRequest{
				Head: &github.PullRequestBranch{
					SHA: github.Ptr(expectedSHA),
				},
			},
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "pull/42")
	if sha != expectedSHA {
		t.Fatalf("expected sha %s, got %s", expectedSHA, sha)
	}
}

func TestGetGhRefSha_NilClient(t *testing.T) {
	_, sha := getGhRefAndSha(context.Background(), nil, "owner/repo", "branch/main")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_InvalidRepoFormat(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient()
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, "invalid-repo", "branch/main")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_BranchNotFound(t *testing.T) {
	fullRepo := "owner/repo"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatchHandler(
			mock.GetReposBranchesByOwnerByRepoByBranch,
			http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusNotFound)
				_, _ = io.Copy(w, strings.NewReader(`{"message": "Not Found"}`))
			}),
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "branch/nonexistent")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_TagNotFound(t *testing.T) {
	fullRepo := "owner/repo"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatchHandler(
			mock.GetReposGitRefByOwnerByRepoByRef,
			http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusNotFound)
				_, _ = io.Copy(w, strings.NewReader(`{"message": "Not Found"}`))
			}),
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "tag/nonexistent")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_PRNotFound(t *testing.T) {
	fullRepo := "owner/repo"

	httpClient := mock.NewMockedHTTPClient(
		mock.WithRequestMatchHandler(
			mock.GetReposPullsByOwnerByRepoByPullNumber,
			http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusNotFound)
				_, _ = io.Copy(w, strings.NewReader(`{"message": "Not Found"}`))
			}),
		),
	)
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, fullRepo, "pull/999")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_InvalidPRNumber(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient()
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, "owner/repo", "pull/abc")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}

func TestGetGhRefSha_UnknownRefFormat(t *testing.T) {
	httpClient := mock.NewMockedHTTPClient()
	ghClient := github.NewClient(httpClient)

	_, sha := getGhRefAndSha(context.Background(), ghClient, "owner/repo", "unknown/format")
	if sha != "" {
		t.Fatalf("expected empty sha, got %s", sha)
	}
}
