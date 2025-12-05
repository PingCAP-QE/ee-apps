package image

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

// imageWorker handles async multi-arch image collection requests and publishing requests.
type imageWorker struct {
	logger      zerolog.Logger
	redisClient redis.Cmdable
	options     struct {
		LarkWebhookURL string
	}
}

// NewWorker creates a new MultiarchWorker instance.
func NewWorker(logger *zerolog.Logger, redisClient redis.Cmdable, options map[string]string) *imageWorker {
	handler := imageWorker{redisClient: redisClient}
	if logger == nil {
		handler.logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
	} else {
		handler.logger = *logger
	}

	handler.options.LarkWebhookURL = options["lark_webhook_url"]

	return &handler
}

func (p *imageWorker) SupportEventTypes() []string {
	return []string{
		EventTypeImageMultiArchCollectRequest,
		EventTypeImagePublishRequest,
	}
}

// Run starts the worker loop to process queued requests.
func (p *imageWorker) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(p.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}

	switch event.Type() {
	case EventTypeImageMultiArchCollectRequest:
		return p.processCollectRequest(context.Background(), event)
	case EventTypeImagePublishRequest:
		return p.processPublishRequest(context.Background(), event)
	default:
		return cloudevents.ResultNACK
	}
}

// processCollectRequest performs the actual multi-arch image collection.
func (w *imageWorker) processCollectRequest(ctx context.Context, event cloudevents.Event) error {
	// Create a context with timeout
	ctxWithTimeout, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()

	return processRequest[image.RequestMultiarchCollectPayload](ctxWithTimeout, event, w.logger, w.redisClient, w.collectMultiArch)
}

// processPublishRequest performs the actual image publishing.
func (w *imageWorker) processPublishRequest(ctx context.Context, event cloudevents.Event) error {
	ctxWithTimeout, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()

	return processRequest[image.RequestToCopyPayload](ctxWithTimeout, event, w.logger, w.redisClient, w.copyImage)
}

// copyImage copies a Docker image from the source registry to the target registry.
//
// When running in k8s pod, it should use the service account that has Docker authentication
// configured and appended to its context.
//
// When debugging locally, it will use the default authentication stored in the
// Docker config.json file (~/.docker/config.json).
func (s *imageWorker) copyImage(ctx context.Context, p *image.RequestToCopyPayload) (string, error) {
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
		return "", err
	}

	l.Info().Msg("Image successfully synced to destination")
	return p.Destination, nil
}

// collectMultiArch performs the actual multi-arch image collection (sync mode).
func (s *imageWorker) collectMultiArch(ctx context.Context, p *image.RequestMultiarchCollectPayload) (*image.RequestMultiarchCollectResult, error) {
	repo, pushedTag, err := share.SplitRepoAndTag(p.ImageURL)
	if err != nil {
		return nil, err
	}

	archTags := listSingleArchTags(repo, pushedTag)
	if len(archTags) < 2 {
		s.logger.Info().Str("image", p.ImageURL).Msg("less than 2 arch tags found, skipping multi-arch manifest creation")
		return nil, nil
	}

	var manifests []string
	for _, archTag := range archTags {
		digest, err := crane.Digest(fmt.Sprintf("%s:%s", repo, archTag), crane.WithContext(ctx))
		if err != nil {
			return nil, err
		}
		manifests = append(manifests, fmt.Sprintf("%s@%s", repo, digest))
	}

	baseTags := computeBaseTags(pushedTag, p.ReleaseTagSuffix)
	if len(manifests) > 1 {
		if _, err := pushMultiarchManifest(repo, baseTags, manifests, nil); err != nil {
			return nil, err
		}
	}

	return &image.RequestMultiarchCollectResult{
		Repo: &repo,
		Tags: baseTags,
	}, nil
}

// processRequest performs the actual action in a generic way.
func processRequest[P any, R any](
	ctx context.Context,
	event cloudevents.Event,
	logger zerolog.Logger,
	redisClient redis.Cmdable,
	processFunc func(context.Context, *P) (R, error),
) error {
	var p P
	if err := event.DataAs(&p); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	requestID := event.ID()
	l := logger.With().Str("request_id", requestID).Logger()

	// Update status to processing
	if err := redisClient.Set(ctx, requestID, share.PublishStateProcessing, share.DefaultStateTTL).Err(); err != nil {
		l.Err(err).Msg("Failed to update status to processing")
		return err
	}

	// Call the generic process function
	result, err := processFunc(ctx, &p)

	// Update final status based on result
	newStatus := share.PublishStateSuccess
	if err != nil {
		newStatus = share.PublishStateFailed
		l.Err(err).Msg("Process failed")
		// TODO: should we push the event to the queue for retry?
	} else {
		l.Info().Any("result", result).Msg("Process completed successfully")
		resultBytes, err := json.Marshal(result)
		if err != nil {
			l.Err(err).Msg("Failed to marshal result")
			return err
		}
		if err := redisClient.Set(context.Background(), fmt.Sprintf("%s-result", requestID), resultBytes, share.DefaultStateTTL).Err(); err != nil {
			l.Err(err).Str("intended_status", newStatus).Msg("Failed to update final status")
		}
	}

	if err := redisClient.Set(context.Background(), requestID, newStatus, share.DefaultStateTTL).Err(); err != nil {
		l.Err(err).Str("intended_status", newStatus).Msg("Failed to update final status")
	}

	return cloudevents.ResultACK
}
