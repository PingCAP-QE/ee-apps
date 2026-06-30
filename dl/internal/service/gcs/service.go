package gcs

import (
	"context"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"

	"cloud.google.com/go/storage"
	"google.golang.org/api/option"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/attachment"
	gcs "github.com/PingCAP-QE/ee-apps/dl/gen/gcs"
	pkggcs "github.com/PingCAP-QE/ee-apps/dl/pkg/gcs"
)

type gcssrvc struct {
	logger *log.Logger
	client *storage.Client
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

func New(logger *log.Logger, cfgFile string) gcs.Service {
	cfgBytes, err := os.ReadFile(cfgFile)
	if err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}

	var cfg pkggcs.Config

	if isJSONFile(cfgFile) {
		cfg.CredentialsJSON = string(cfgBytes)
	} else {
		if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
			logger.Fatalf("Failed to load configuration: %v", err)
		}
	}

	client, err := newClient(context.Background(), &cfg)
	if err != nil {
		logger.Fatalf("Failed to create GCS client: %v", err)
	}

	return &gcssrvc{logger: logger, client: client}
}

func (s *gcssrvc) DownloadObject(ctx context.Context, p *gcs.DownloadObjectPayload) (res *gcs.DownloadObjectResult, resp io.ReadCloser, err error) {
	obj := s.client.Bucket(p.Bucket).Object(p.Key)
	attrs, err := obj.Attrs(ctx)
	if err != nil {
		return nil, nil, err
	}

	reader, err := obj.NewReader(ctx)
	if err != nil {
		return nil, nil, err
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
