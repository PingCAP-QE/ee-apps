package impl

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"slices"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/ks3sdklib/aws-sdk-go/aws"
	"github.com/ks3sdklib/aws-sdk-go/aws/credentials"
	"github.com/ks3sdklib/aws-sdk-go/service/s3"
	"github.com/rs/zerolog"
)

const defaultMaxKS3Retries = 3

type fsWorker struct {
	logger      zerolog.Logger
	redisClient redis.Cmdable
	options     struct {
		LarkWebhookURL string
		S3             struct {
			BucketName string
		}
	}
	s3Client *s3.S3
}

func NewFsWorker(logger *zerolog.Logger, redisClient redis.Cmdable, options map[string]string) (*fsWorker, error) {
	handler := fsWorker{redisClient: redisClient}
	if logger == nil {
		handler.logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
	} else {
		handler.logger = *logger
	}
	handler.options.LarkWebhookURL = options["lark_webhook_url"]
	handler.options.S3.BucketName = options["s3.bucket_name"]

	cre := credentials.NewStaticCredentials(
		options["s3.access_key"],
		options["s3.secret_key"],
		options["s3.session_token"])
	handler.s3Client = s3.New(&aws.Config{
		Credentials: cre,
		Region:      options["s3.region"],   // Ref: https://docs.ksyun.com/documents/6761
		Endpoint:    options["s3.endpoint"], // Ref: https://docs.ksyun.com/documents/6761
		MaxRetries:  defaultMaxKS3Retries,
		Logger:      logger,
	})
	// Enable KS3 log when debug mode is enabled.
	if logger.GetLevel() <= zerolog.DebugLevel {
		handler.s3Client.Config.LogLevel = aws.LogOn
	}

	return &handler, nil
}

func (p *fsWorker) SupportEventTypes() []string {
	return []string{EventTypeFsPublishRequest}
}

// Handle for test case run events
func (p *fsWorker) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(p.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}
	p.redisClient.SetXX(context.Background(), event.ID(), PublishStateProcessing, redis.KeepTTL)

	data := new(PublishRequest)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	result := p.handle(data)
	switch {
	case cloudevents.IsACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateSuccess, redis.KeepTTL)
	case cloudevents.IsNACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateFailed, redis.KeepTTL)
		p.notifyLark(&data.Publish, result)
	default:
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateCanceled, redis.KeepTTL)
	}

	return result
}

func (p *fsWorker) handle(data *PublishRequest) cloudevents.Result {
	// 1. get the the file content from data.From.
	err := doWithOCIFile(data.From.Oci, func(input io.Reader) error {
		return doWithTempFileFromReader(input, func(inputF *os.File) error {
			// 2. publish the tarball.
			return p.publish(inputF, &data.Publish)
		})
	})
	if err != nil {
		p.logger.Err(err).Msg("publish to fileserver failed")
		return cloudevents.NewReceipt(false, "publish to fileserver failed: %v", err)
	}
	p.logger.Info().Msg("publish to fileserver success")

	return cloudevents.ResultACK
}

func (p *fsWorker) notifyLark(publishInfo *PublishInfo, err error) {
	if p.options.LarkWebhookURL == "" {
		return
	}

	message := fmt.Sprintf("Failed to publish %s/%s/%s file to fileserver: %v",
		publishInfo.Name,
		publishInfo.Version,
		publishInfo.EntryPoint,
		err)

	payload := map[string]interface{}{
		"msg_type": "text",
		"content": map[string]string{
			"text": message,
		},
	}

	jsonPayload, err := json.Marshal(payload)
	if err != nil {
		p.logger.Err(err).Msg("failed to marshal JSON payload")
	}

	resp, err := http.Post(p.options.LarkWebhookURL, "application/json", bytes.NewBuffer(jsonPayload))
	if err != nil {
		p.logger.Err(err).Msg("failed to send notification to Lark")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		p.logger.Error().Msgf("Lark API returned non-OK status: %d", resp.StatusCode)
	}
}

func (p *fsWorker) publish(content io.ReadSeeker, info *PublishInfo) error {
	keys := targetFsFullPaths(info)
	if len(keys) == 0 {
		return nil
	}

	bucketName := p.options.S3.BucketName

	// upload the artifact files to KS3 bucket.
	for _, key := range keys {
		_, err := p.s3Client.PutObject(&s3.PutObjectInput{
			Bucket: aws.String(bucketName),
			Key:    aws.String(key),
			Body:   content,
		})
		if err != nil {
			p.logger.
				Err(err).
				Str("bucket", bucketName).
				Str("key", key).
				Msg("failed to upload file to KS3 bucket.")
			return err
		}
	}

	// update git ref sha: download/refs/pingcap/<comp>/<branch>/sha1
	refKV := targetFsRefKeyValue(info)
	_, err := p.s3Client.PutObject(&s3.PutObjectInput{
		Bucket: aws.String(bucketName),
		Key:    aws.String(refKV[0]),
		Body:   bytes.NewReader([]byte(refKV[1])),
	})
	if err != nil {
		p.logger.
			Err(err).
			Str("bucket", bucketName).
			Str("key", refKV[0]).
			Msg("failed to upload content in KS3 bucket.")
		return err
	}

	return nil
}
