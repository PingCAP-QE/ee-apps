package impl_test

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
)

func TestSyncImage_Integration(t *testing.T) {
	// Skip if running in CI or want to skip integration tests
	if testing.Short() {
		t.Skip("Skipping integration test")
	}

	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	service := impl.NewArtifact(&logger)

	t.Run("sync real image to ttl.sh", func(t *testing.T) {
		// Setup
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()

		// Use a small image for faster tests
		sourceImage := "alpine:latest"

		// ttl.sh creates a random repository with 24h expiration
		// Use a unique name to avoid conflicts
		randomSuffix := time.Now().UnixNano()
		targetImage := fmt.Sprintf("ttl.sh/test-sync-%d:1h", randomSuffix)

		req := &artifact.ImageSyncRequest{
			Source: sourceImage,
			Target: targetImage,
		}

		// Execute
		resp, err := service.SyncImage(ctx, req)

		// Verify
		assert.NoError(t, err)
		assert.Equal(t, req, resp)

		// Log the target image for manual verification if needed
		t.Logf("Successfully pushed image to: %s", targetImage)

		// Optional: Verify the image exists in the registry
		// This would require additional code to check if the image was pushed correctly
	})

	t.Run("sync non-existent image should fail", func(t *testing.T) {
		// Setup
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		sourceImage := "debian:non-existent-tag-12345"
		targetImage := "ttl.sh/test-sync-should-fail:1h"

		req := &artifact.ImageSyncRequest{
			Source: sourceImage,
			Target: targetImage,
		}

		// Execute
		resp, err := service.SyncImage(ctx, req)

		// Verify
		assert.Error(t, err)
		assert.Nil(t, resp)

		// Check that we got an HTTP error
		httpErr, ok := err.(*artifact.HTTPError)
		assert.True(t, ok)
		assert.Contains(t, httpErr.Message, "failed to sync image")
	})
}
