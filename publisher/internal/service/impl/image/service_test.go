package image

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

// TestImageServiceCopyFlow tests the full workflow of requesting an image copy and
// checking its status. It uses ttl.sh as both source and destination registries.
func TestImageServiceCopyFlow(t *testing.T) {
	// Setup miniredis (in-memory Redis for testing)
	mr, err := miniredis.Run()
	require.NoError(t, err)
	defer mr.Close()

	// Create Redis client connected to miniredis
	redisClient := redis.NewClient(&redis.Options{
		Addr: mr.Addr(),
	})
	defer redisClient.Close()

	// Setup logger
	logger := zerolog.New(zerolog.NewConsoleWriter())

	// Create the service with a relatively short timeout
	service := &imagesrvc{
		BaseService: &share.BaseService{
			Logger:      &logger,
			KafkaWriter: nil,
			RedisClient: redisClient,
			EventSource: "test",
		},
	}

	// Setup test image names
	sourceImage := "ttl.sh/pingcap-test-source:1h"
	destinationImage := "ttl.sh/pingcap-test-destination:1h"

	// Push a minimal test image to ttl.sh as our source
	t.Log("Pushing test image to source registry")
	err = setupTestImage(sourceImage)
	require.NoError(t, err, "Failed to push test image to ttl.sh")

	// Create context with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()

	// Test RequestToCopy
	t.Log("Testing RequestToCopy")
	requestID, err := service.RequestToCopy(ctx, &image.RequestToCopyPayload{
		Source:      sourceImage,
		Destination: destinationImage,
	})
	require.NoError(t, err, "RequestToCopy should not return an error")
	require.NotEmpty(t, requestID, "RequestID should not be empty")

	// Test QueryCopyingStatus
	// Initially the status should be "queued" or "processing"
	t.Log("Testing initial status")
	status, err := service.QueryCopyingStatus(ctx, &image.QueryCopyingStatusPayload{
		RequestID: requestID,
	})
	require.NoError(t, err, "QueryCopyingStatus should not return an error")
	assert.Contains(t, []string{share.PublishStateQueued, share.PublishStateProcessing}, status,
		"Initial status should be 'queued' or 'processing'")

	// Poll for completion (with timeout)
	t.Log("Waiting for copy operation to complete")
	success := pollForCompletion(t, ctx, service, requestID)
	require.True(t, success, "Copy operation should complete successfully")

	// Verify the image was copied to destination
	t.Log("Verifying image was copied to destination")
	exists, err := imageExists(destinationImage)
	require.NoError(t, err, "Checking image existence should not fail")
	assert.True(t, exists, "Image should exist in destination registry")
}

// setupTestImage creates and pushes a minimal test image to the specified registry path
func setupTestImage(imagePath string) error {
	// Create a minimal image
	img, err := crane.Image(map[string][]byte{
		// Empty layer with basic configuration
		"/": []byte(""),
	})
	if err != nil {
		return err
	}

	// Push the image to the registry
	return crane.Push(img, imagePath)
}

// imageExists checks if an image exists in the registry
func imageExists(imagePath string) (bool, error) {
	// Try to pull the image metadata - if it works, the image exists
	_, err := crane.Head(imagePath)
	if err != nil {
		return false, nil // Assume not found rather than returning error
	}
	return true, nil
}

// pollForCompletion polls the status until it reaches a terminal state
func pollForCompletion(t *testing.T, ctx context.Context, svc image.Service, requestID string) bool {
	timeout := time.After(2 * time.Minute)
	tick := time.Tick(2 * time.Second)

	for {
		select {
		case <-timeout:
			t.Log("Timed out waiting for copy to complete")
			return false
		case <-tick:
			status, err := svc.QueryCopyingStatus(ctx, &image.QueryCopyingStatusPayload{
				RequestID: requestID,
			})
			if err != nil {
				t.Logf("Error checking status: %v", err)
				continue
			}

			t.Logf("Current status: %s", status)

			// Check for terminal states
			if status == share.PublishStateSuccess {
				return true
			} else if status == share.PublishStateFailed {
				return false
			}
			// Otherwise continue polling
		}
	}
}
