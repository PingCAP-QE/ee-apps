package impl_test

import (
	"context"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
)

// TestBumpForTidbx_Integration tests the complete flow of creating a hotfix tag
func TestBumpForTidbx_Integration(t *testing.T) {
	// Skip if running in CI or want to skip integration tests
	if testing.Short() {
		t.Skip("Skipping integration test")
	}

	// This test requires a valid GitHub token and repository access
	// It's meant to be run manually with proper credentials
	t.Skip("Integration test requires GitHub credentials and proper repository access")

	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	ghToken := os.Getenv("GITHUB_TOKEN") // Use environment variable for GitHub token
	if ghToken == "" {
		t.Skip("GITHUB_TOKEN environment variable not set")
	}
	ghClient := impl.NewGitHubClient(ghToken)
	service := impl.NewHotfix(&logger, ghClient)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	t.Run("create tag with branch only", func(t *testing.T) {
		branch := "main"
		req := &hotfix.BumpTagForTidbxPayload{
			Repo:   "owner/repo",
			Branch: &branch,
			Author: "test-user",
		}

		result, err := service.BumpTagForTidbx(ctx, req)
		assert.NoError(t, err)
		assert.NotNil(t, result)
		assert.Equal(t, req.Repo, result.Repo)
		assert.NotEmpty(t, result.Commit)
		assert.NotEmpty(t, result.Tag)
	})
}

// TestBumpTagForTidbx_Validation tests input validation
func TestBumpTagForTidbx_Validation(t *testing.T) {
	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	ghClient := github.NewClient(nil)
	service := impl.NewHotfix(&logger, ghClient)

	ctx := context.Background()

	t.Run("invalid repository format", func(t *testing.T) {
		branch := "main"
		req := &hotfix.BumpTagForTidbxPayload{
			Repo:   "invalid-repo-format",
			Branch: &branch,
			Author: "test-user",
		}

		result, err := service.BumpTagForTidbx(ctx, req)
		assert.Error(t, err)
		assert.Nil(t, result)

		httpErr, ok := err.(*hotfix.HTTPError)
		assert.True(t, ok)
		assert.Equal(t, http.StatusBadRequest, httpErr.Code)
		assert.Contains(t, httpErr.Message, "invalid repository format")
	})

	t.Run("missing both branch and commit", func(t *testing.T) {
		req := &hotfix.BumpTagForTidbxPayload{
			Repo:   "owner/repo",
			Author: "test-user",
		}

		result, err := service.BumpTagForTidbx(ctx, req)
		assert.Error(t, err)
		assert.Nil(t, result)

		httpErr, ok := err.(*hotfix.HTTPError)
		assert.True(t, ok)
		assert.Equal(t, http.StatusBadRequest, httpErr.Code)
		assert.Contains(t, httpErr.Message, "at least one of 'branch' or 'commit' must be provided")
	})

	t.Run("non-existent repository", func(t *testing.T) {
		branch := "main"
		req := &hotfix.BumpTagForTidbxPayload{
			Repo:   "nonexistent/repo",
			Branch: &branch,
			Author: "test-user",
		}

		result, err := service.BumpTagForTidbx(ctx, req)
		// This will fail when trying to access GitHub API
		assert.Error(t, err)
		assert.Nil(t, result)
	})
}
