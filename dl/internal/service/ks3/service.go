package ks3

import (
	"context"
	"io"
	"log"
	"os"
	"path/filepath"

	"github.com/ks3sdklib/aws-sdk-go/aws"
	"github.com/ks3sdklib/aws-sdk-go/aws/credentials"
	"github.com/ks3sdklib/aws-sdk-go/service/s3"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/attachment"
	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
	pkgks3 "github.com/PingCAP-QE/ee-apps/dl/pkg/ks3"
)

// ks3srvc implements the KS3 service.
type ks3srvc struct {
	logger *log.Logger
	client *s3.S3
}

func newClient(cfg *pkgks3.Config) *s3.S3 {
	var cre *credentials.Credentials
	if cfg != nil && cfg.AccessKey != "" && cfg.SecretKey != "" {
		cre = credentials.NewStaticCredentials(cfg.AccessKey, cfg.SecretKey, "")
	}
	awsConfig := aws.Config{
		Region:           cfg.Region,
		Credentials:      cre,
		Endpoint:         cfg.Endpoint,
		DisableSSL:       true,
		LogLevel:         0,
		LogHTTPBody:      false,
		S3ForcePathStyle: false,
		Logger:           nil,
	}

	return s3.New(&awsConfig)
}

// New returns the ks3 service implementation.
func New(logger *log.Logger, cfgFile string) ks3.Service {
	var cfg pkgks3.Config

	cfgBytes, err := os.ReadFile(cfgFile)
	if err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}
	if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}

	return &ks3srvc{logger: logger, client: newClient(&cfg)}
}

// HeadObject checks if an object exists in KS3 without downloading.
func (s *ks3srvc) HeadObject(bucket, key string) (*s3.HeadObjectOutput, error) {
	headParams := &s3.HeadObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(key),
	}
	return s.client.HeadObject(headParams)
}

// DownloadObject implements download-object.
func (s *ks3srvc) DownloadObject(ctx context.Context, p *ks3.DownloadObjectPayload) (res *ks3.DownloadObjectResult, resp io.ReadCloser, err error) {
	getParams := &s3.GetObjectInput{
		Bucket: aws.String(p.Bucket),
		Key:    aws.String(p.Key),
	}

	getObjectOutput, err := s.client.GetObject(getParams)
	if err != nil {
		return nil, nil, err
	}

	res = &ks3.DownloadObjectResult{}
	if getObjectOutput != nil {
		if getObjectOutput.ContentLength != nil {
			res.Length = *getObjectOutput.ContentLength
		}
		if getObjectOutput.ContentDisposition != nil {
			res.ContentDisposition = *getObjectOutput.ContentDisposition
		} else {
			res.ContentDisposition = attachment.ContentDisposition(filepath.Base(p.Key))
		}
	}

	return res, getObjectOutput.Body, nil
}
