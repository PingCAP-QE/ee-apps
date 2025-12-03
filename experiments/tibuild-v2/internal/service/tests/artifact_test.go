package impl_test

import (
	"context"
	"fmt"
	"log"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/registry"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
)

func TestSyncImage_Integration(t *testing.T) {
	// Skip if running in CI or want to skip integration tests
	if testing.Short() {
		t.Skip("Skipping integration test")
	}

	ctx := context.Background()

	// Start the registry container
	registryContainer, err := registry.Run(ctx, "registry:2.8.3")
	if err != nil {
		log.Fatalf("failed to start registry container: %v", err)
	}

	t.Cleanup(func() {
		if err := testcontainers.TerminateContainer(registryContainer); err != nil {
			log.Fatalf("failed to terminate registry container: %v", err)
		}
	})

	// Get the registry URL (e.g., "localhost:5000")
	registryURL, err := registryContainer.Address(t.Context())
	if err != nil {
		t.Fatal(err)
	}

	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	service := impl.NewArtifact(&logger)

	t.Run("sync real image", func(t *testing.T) {
		// Setup
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()

		// Use a small image for faster tests
		sourceImage := "alpine:latest"

		// Use a unique name to avoid conflicts
		randomSuffix := time.Now().UnixNano()
		targetImage := fmt.Sprintf("%s/test-sync-%d:1h", registryURL, randomSuffix)

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
		targetImage := fmt.Sprintf("%s/test-sync-should-fail:non-existent-tag-12345", registryURL)

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
