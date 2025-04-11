package impl

import (
	"context"
	"fmt"
	"time"

	"github.com/go-redis/redis/v8"
	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/google/uuid"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
)

// NewImage returns the image service implementation.
func NewImage(logger *zerolog.Logger, redisClient *redis.Client, timeout time.Duration) image.Service {
	return &imagesrvc{
		logger:      logger,
		redisClient: redisClient,
		timeout:     timeout,
	}
}

// artifact service example implementation.
// The example methods log the requests and return zero values.
type imagesrvc struct {
	logger      *zerolog.Logger
	redisClient *redis.Client
	timeout     time.Duration
}

// QueryCopyingStatus implements image.Service.
func (s *imagesrvc) QueryCopyingStatus(ctx context.Context, p *image.QueryCopyingStatusPayload) (string, error) {
	status, err := s.redisClient.Get(ctx, p.RequestID).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("request ID not found")
		}
		return "", fmt.Errorf("failed to get status from Redis: %v", err)
	}

	return status, nil
}

// RequestToCopy implements image.Service.
func (s *imagesrvc) RequestToCopy(ctx context.Context, p *image.RequestToCopyPayload) (res string, err error) {
	// 1. generate a unique request ID
	requestID := uuid.New().String()

	// 2. init the status in redis.
	if err := s.redisClient.SetNX(ctx, requestID, PublishStateQueued, DefaultStateTTL).Err(); err != nil {
		return "", fmt.Errorf("failed to initialize request status: %v", err)
	}

	s.logger.Info().
		Str("request_id", requestID).
		Str("source", p.Source).
		Str("destination", p.Destination).
		Msg("Image copy request queued")

	// 3. async call the copyImage method and wait the result and update the status in redis.
	go func() {
		// Create a context with timeout
		ctxWithTimeout, cancel := context.WithTimeout(context.Background(), s.timeout)
		defer cancel()

		l := s.logger.With().Str("request_id", requestID).Logger()

		// Update status to processing
		if err := s.redisClient.Set(ctxWithTimeout, requestID, PublishStateProcessing, DefaultStateTTL).Err(); err != nil {
			l.Err(err).Msg("Failed to update status to processing")
			return
		}

		// Copy the image
		err := s.copyImage(ctxWithTimeout, p)

		// Update final status based on result
		newStatus := PublishStateSuccess
		if err != nil {
			newStatus = PublishStateFailed
			l.Err(err).Msg("Image copy failed")
		} else {
			l.Info().Msg("Image copy completed successfully")
		}

		if err := s.redisClient.Set(context.Background(), requestID, newStatus, DefaultStateTTL).Err(); err != nil {
			l.Err(err).Str("intended_status", newStatus).Msg("Failed to update final status")
		}
	}()

	return requestID, nil
}

// copyImage copies a Docker image from the source registry to the target registry.
//
// When running in k8s pod, it should use the service account that has Docker authentication
// configured and appended to its context.
//
// When debugging locally, it will use the default authentication stored in the
// Docker config.json file (~/.docker/config.json).
func (s *imagesrvc) copyImage(ctx context.Context, p *image.RequestToCopyPayload) error {
	l := s.logger.With().
		Str("source", p.Source).
		Str("destination", p.Destination).
		Logger()

	l.Info().Msg("Syncing Docker image")

	// Create options for crane operations
	options := []crane.Option{
		crane.WithContext(ctx),
	}

	// Use the crane library to copy the image directly between registries
	if err := crane.Copy(p.Source, p.Destination, options...); err != nil {
		l.Err(err).Msg("Failed to sync image")
		return err
	}

	l.Info().Msg("Image successfully synced to DockerHub")
	return nil
}
