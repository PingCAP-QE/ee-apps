package gcs

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"cloud.google.com/go/storage"
	"google.golang.org/api/option"
	"gopkg.in/yaml.v3"

	gcs "github.com/PingCAP-QE/ee-apps/dl/gen/gcs"
	"github.com/PingCAP-QE/ee-apps/dl/pkg/attachment"
	pkggcs "github.com/PingCAP-QE/ee-apps/dl/pkg/gcs"
)

var errGCSNotConfigured = errors.New("GCS is not configured")

type gcssrvc struct {
	logger          *log.Logger
	client          *storage.Client
	allowedBuckets  []string
	allowedPrefixes []string
	mu              sync.RWMutex
}

func newClient(ctx context.Context, cfg *pkggcs.Config) (*storage.Client, error) {
	var opts []option.ClientOption
	if cfg != nil {
		if cfg.CredentialsFile != "" {
			opts = append(opts, option.WithCredentialsFile(cfg.CredentialsFile))
		}
		if cfg.CredentialsJSON != "" {
			opts = append(opts, option.WithCredentialsJSON([]byte(cfg.CredentialsJSON)))
		}
	}
	return storage.NewClient(ctx, opts...)
}

func isJSONFile(filename string) bool {
	return strings.HasSuffix(filename, ".json")
}

func New(logger *log.Logger, cfgFile string, allowedBuckets, allowedPrefixes []string) gcs.Service {
	var cfg pkggcs.Config

	if cfgFile != "" {
		cfgBytes, err := os.ReadFile(cfgFile)
		if err != nil {
			logger.Printf("Config file %q not found, GCS service unavailable", cfgFile)
			return &gcssrvc{logger: logger, allowedBuckets: allowedBuckets, allowedPrefixes: allowedPrefixes}
		}

		if isJSONFile(cfgFile) {
			cfg.CredentialsJSON = string(cfgBytes)
		} else {
			if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
				logger.Fatalf("Failed to load configuration: %v", err)
			}
		}
	}

	client, err := newClient(context.Background(), &cfg)
	if err != nil {
		logger.Printf("Failed to create GCS client: %v", err)
		return &gcssrvc{logger: logger, allowedBuckets: allowedBuckets, allowedPrefixes: allowedPrefixes}
	}

	return &gcssrvc{
		logger:          logger,
		client:          client,
		allowedBuckets:  allowedBuckets,
		allowedPrefixes: allowedPrefixes,
	}
}

func (s *gcssrvc) getClient() (*storage.Client, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.client == nil {
		return nil, errGCSNotConfigured
	}
	return s.client, nil
}

// Reload re-reads the config file and recreates the GCS client. It is safe for
// concurrent use with DownloadObject and HeadObject.
func (s *gcssrvc) Reload(ctx context.Context, cfgFile string) error {
	var cfg pkggcs.Config
	cfgBytes, err := os.ReadFile(cfgFile)
	if err != nil {
		return fmt.Errorf("failed to read config file %q: %w", cfgFile, err)
	}
	if isJSONFile(cfgFile) {
		cfg.CredentialsJSON = string(cfgBytes)
	} else {
		if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
			return fmt.Errorf("failed to parse config file %q: %w", cfgFile, err)
		}
	}
	client, err := newClient(ctx, &cfg)
	if err != nil {
		return fmt.Errorf("failed to create GCS client from %q: %w", cfgFile, err)
	}

	s.mu.Lock()
	old := s.client
	s.client = client
	s.mu.Unlock()

	if old != nil {
		old.Close()
	}

	s.logger.Printf("GCS client reloaded from %q", cfgFile)
	return nil
}

func (s *gcssrvc) DownloadObject(ctx context.Context, p *gcs.DownloadObjectPayload) (res *gcs.DownloadObjectResult, resp io.ReadCloser, err error) {
	if err := ValidateBucket(p.Bucket, s.allowedBuckets); err != nil {
		return nil, nil, gcs.MakeInvalidFilePath(err)
	}
	if err := ValidateObjectKey(p.Key, s.allowedPrefixes); err != nil {
		return nil, nil, gcs.MakeInvalidFilePath(err)
	}

	client, err := s.getClient()
	if err != nil {
		return nil, nil, err
	}

	obj := client.Bucket(p.Bucket).Object(p.Key)
	attrs, err := obj.Attrs(ctx)
	if err != nil {
		return nil, nil, gcs.MakeInvalidFilePath(sanitizeGCSError(err))
	}

	reader, err := obj.NewReader(ctx)
	if err != nil {
		return nil, nil, gcs.MakeInvalidFilePath(sanitizeGCSError(err))
	}

	res = &gcs.DownloadObjectResult{
		Length: attrs.Size,
	}
	if attrs.ContentDisposition != "" {
		res.ContentDisposition = attrs.ContentDisposition
	} else {
		res.ContentDisposition = attachment.ContentDisposition(filepath.Base(p.Key))
	}

	return res, reader, nil
}

// HeadObject implements head-object.
func (s *gcssrvc) HeadObject(ctx context.Context, p *gcs.HeadObjectPayload) (*gcs.HeadObjectResult, error) {
	if err := ValidateBucket(p.Bucket, s.allowedBuckets); err != nil {
		return nil, gcs.MakeInvalidFilePath(err)
	}
	if err := ValidateObjectKey(p.Key, s.allowedPrefixes); err != nil {
		return nil, gcs.MakeInvalidFilePath(err)
	}

	client, err := s.getClient()
	if err != nil {
		return nil, err
	}

	obj := client.Bucket(p.Bucket).Object(p.Key)
	attrs, err := obj.Attrs(ctx)
	if err != nil {
		return nil, gcs.MakeInvalidFilePath(sanitizeGCSError(err))
	}
	res := &gcs.HeadObjectResult{
		Length: attrs.Size,
	}
	if attrs.ContentDisposition != "" {
		res.ContentDisposition = attrs.ContentDisposition
	} else {
		res.ContentDisposition = attachment.ContentDisposition(filepath.Base(p.Key))
	}
	return res, nil
}
