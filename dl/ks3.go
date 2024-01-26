package dl

import (
	"context"
	"io"
	"log"
	"net/url"
	"os"
	"path/filepath"

	"github.com/ks3sdklib/aws-sdk-go/aws"
	"github.com/ks3sdklib/aws-sdk-go/aws/credentials"
	"github.com/ks3sdklib/aws-sdk-go/service/s3"
	"gopkg.in/yaml.v3"

	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
	pkgks3 "github.com/PingCAP-QE/ee-apps/dl/pkg/ks3"
)

// ks3 service example implementation.
// The example methods log the requests and return zero values.
type ks3srvc struct {
	logger *log.Logger
	client *s3.S3
}

func newKS3Client(cfg *pkgks3.Config) *s3.S3 {
	var cre = credentials.NewStaticCredentials(cfg.AccessKey, cfg.SecretKey, "")
	awsConfig := aws.Config{
		Region:           cfg.Region, // Ref: https://docs.ksyun.com/documents/6761
		Credentials:      cre,
		Endpoint:         cfg.Endpoint, // Ref: https://docs.ksyun.com/documents/6761
		DisableSSL:       true,
		LogLevel:         0,
		LogHTTPBody:      false,
		S3ForcePathStyle: false,
		Logger:           nil,
	}

	return s3.New(&awsConfig)
}

// NewKs3 returns the ks3 service implementation.
func NewKs3(logger *log.Logger, cfgFile string) ks3.Service {
	var cfg pkgks3.Config

	cfgBytes, err := os.ReadFile(cfgFile)
	if err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}
	if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}

	return &ks3srvc{logger: logger, client: newKS3Client(&cfg)}
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
			res.ContentDisposition = `attachment; filename*=UTF-8''` + url.QueryEscape(filepath.Base(p.Key))
		}
	}

	return res, getObjectOutput.Body, nil
}
