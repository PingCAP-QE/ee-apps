package ks3

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/ks3sdklib/aws-sdk-go/aws"
	"github.com/ks3sdklib/aws-sdk-go/aws/credentials"
	"github.com/ks3sdklib/aws-sdk-go/service/s3"
	"gopkg.in/yaml.v3"

	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
	"github.com/PingCAP-QE/ee-apps/dl/pkg/attachment"
	pkgks3 "github.com/PingCAP-QE/ee-apps/dl/pkg/ks3"
)

// scope is the bucket/key allowlist currently in force.
//
// The request names both the bucket and the key, and the service fetches them
// with its own credentials, so the pair is a confused-deputy primitive unless it
// is checked. The zero value allows nothing.
type scope struct {
	buckets  []string
	prefixes []string
}

func newScope(cfg pkgks3.Config) scope {
	normalize := func(in []string) []string {
		out := make([]string, 0, len(in))
		for _, entry := range in {
			if trimmed := trimLower(entry); trimmed != "" {
				out = append(out, trimmed)
			}
		}
		return out
	}
	return scope{buckets: normalize(cfg.AllowedBuckets), prefixes: normalize(cfg.AllowedKeyPrefixes)}
}

func (s scope) allows(bucket, key string) error {
	bucket = trimLower(bucket)
	if bucket == "" {
		return fmt.Errorf("bucket is required")
	}
	allowed := false
	for _, candidate := range s.buckets {
		if candidate == bucket {
			allowed = true
			break
		}
	}
	if !allowed {
		return fmt.Errorf("bucket %q is not in the KS3 allowlist", bucket)
	}
	if len(s.prefixes) == 0 {
		return nil
	}
	key = trimLower(key)
	for _, prefix := range s.prefixes {
		if hasPrefix(key, prefix) {
			return nil
		}
	}
	return fmt.Errorf("key %q is outside the allowed KS3 key prefixes", key)
}

// ks3srvc implements the KS3 service.
type ks3srvc struct {
	logger *log.Logger
	mu     sync.RWMutex
	client *s3.S3
	scope  scope
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
		DisableSSL:       cfg.DisableSSL,
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
	return &ks3srvc{logger: logger, client: newClient(&cfg), scope: newScope(cfg)}
}

func (s *ks3srvc) getClient() *s3.S3 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.client
}

func (s *ks3srvc) getScope() scope {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.scope
}

// Reload re-reads the config file and recreates the KS3 client. It is safe for
// concurrent use with DownloadObject and HeadObject.
func (s *ks3srvc) Reload(ctx context.Context, cfgFile string) error {
	var cfg pkgks3.Config
	cfgBytes, err := os.ReadFile(cfgFile)
	if err != nil {
		return fmt.Errorf("failed to read config file %q: %w", cfgFile, err)
	}
	if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
		return fmt.Errorf("failed to parse config file %q: %w", cfgFile, err)
	}

	s.mu.Lock()
	s.client = newClient(&cfg)
	s.scope = newScope(cfg)
	s.mu.Unlock()

	s.logger.Printf("KS3 client reloaded from %q", cfgFile)
	return nil
}

// DownloadObject implements download-object.
func (s *ks3srvc) DownloadObject(ctx context.Context, p *ks3.DownloadObjectPayload) (res *ks3.DownloadObjectResult, resp io.ReadCloser, err error) {
	if err := s.getScope().allows(p.Bucket, p.Key); err != nil {
		return nil, nil, err
	}
	getParams := &s3.GetObjectInput{
		Bucket: aws.String(p.Bucket),
		Key:    aws.String(p.Key),
	}

	getObjectOutput, err := s.getClient().GetObject(getParams)
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

// HeadObject implements head-object.
func (s *ks3srvc) HeadObject(ctx context.Context, p *ks3.HeadObjectPayload) (*ks3.HeadObjectResult, error) {
	if err := s.getScope().allows(p.Bucket, p.Key); err != nil {
		return nil, err
	}
	output, err := s.getClient().HeadObject(&s3.HeadObjectInput{
		Bucket: aws.String(p.Bucket),
		Key:    aws.String(p.Key),
	})
	if err != nil {
		return nil, err
	}
	res := &ks3.HeadObjectResult{}
	if output.ContentLength != nil {
		res.Length = *output.ContentLength
	}
	if output.ContentDisposition != nil {
		res.ContentDisposition = *output.ContentDisposition
	} else {
		res.ContentDisposition = attachment.ContentDisposition(filepath.Base(p.Key))
	}
	return res, nil
}

func trimLower(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

func hasPrefix(s, prefix string) bool { return strings.HasPrefix(s, prefix) }
