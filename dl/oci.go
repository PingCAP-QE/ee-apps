package dl

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"regexp"
	"strings"

	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	pkgoci "github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
	"gopkg.in/yaml.v3"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/retry"
)

// oci service example implementation.
// The example methods log the requests and return zero values.
type ocisrvc struct {
	logger     *log.Logger
	credential *auth.Credential
}

// NewOci returns the oci service implementation.
func NewOci(logger *log.Logger, cfgFile *string) oci.Service {
	var cfg pkgoci.Config
	if cfgFile == nil {
		return &ocisrvc{logger: logger, credential: &auth.EmptyCredential}
	}

	cfgBytes, err := os.ReadFile(*cfgFile)
	if err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}
	if err := yaml.Unmarshal(cfgBytes, &cfg); err != nil {
		logger.Fatalf("Failed to load configuration: %v", err)
	}

	return &ocisrvc{logger: logger, credential: &auth.Credential{
		Username: cfg.Username,
		Password: cfg.Password,
	}}
}

// ListFiles implements list-files.
func (s *ocisrvc) ListFiles(ctx context.Context, p *oci.ListFilesPayload) (res []string, err error) {
	s.logger.Print("oci.list-files")

	repository, err := s.getTargetRepo(p.Repository)
	if err != nil {
		return nil, err
	}

	files, err := pkgoci.ListFiles(ctx, repository, p.Tag)
	if err != nil {
		return nil, oci.MakeInvalidFilePath(err)
	}

	return files, nil
}

func (s *ocisrvc) getTargetRepo(repo string) (*remote.Repository, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return nil, err
	}

	reg := strings.SplitN(repo, "/", 2)[0]
	repository.Client = &auth.Client{
		Client:     retry.DefaultClient,
		Cache:      auth.DefaultCache,
		Credential: auth.StaticCredential(reg, *s.credential),
	}

	return repository, nil
}

// DownloadFile implements download-file.
func (s *ocisrvc) DownloadFile(ctx context.Context, p *oci.DownloadFilePayload) (res *oci.DownloadFileResult, resp io.ReadCloser, err error) {
	s.logger.Print("oci.download-file")

	repository, err := s.getTargetRepo(p.Repository)
	if err != nil {
		return nil, nil, err
	}

	if p.File != nil {
		return s.downloadFile(ctx, repository, p.Tag, *p.File)
	}

	if p.FileRegex != nil {
		pattern, err := regexp.Compile(*p.FileRegex)
		if err != nil {
			return nil, nil, oci.MakeInvalidFilePath(err)
		}

		files, err := pkgoci.ListFiles(ctx, repository, p.Tag)
		if err != nil {
			return nil, nil, oci.MakeInvalidFilePath(err)
		}

		for _, file := range files {
			if pattern.MatchString(file) {
				return s.downloadFile(ctx, repository, p.Tag, file)
			}
		}

		return nil, nil, oci.MakeInvalidFilePath(fmt.Errorf("could not locate file for download: %s", *p.FileRegex))
	}

	return nil, nil, oci.MakeInvalidFilePath(errors.New("none `file` or `file_regex` param given"))
}

func (s *ocisrvc) downloadFile(ctx context.Context, repo *remote.Repository, tag, file string) (res *oci.DownloadFileResult, resp io.ReadCloser, err error) {
	rc, length, err := pkgoci.NewFileReadCloser(ctx, repo, tag, file)
	if err != nil {
		return nil, nil, err
	}

	res = &oci.DownloadFileResult{
		Length:             length,
		ContentDisposition: `attachment; filename*=UTF-8''` + url.QueryEscape(file),
	}
	return res, rc, nil
}

// DownloadFileSha256 implements download-file-sha256.
func (s *ocisrvc) DownloadFileSha256(ctx context.Context, p *oci.DownloadFileSha256Payload) (res *oci.DownloadFileSha256Result, resp io.ReadCloser, err error) {
	s.logger.Print("oci.download-file-sha256")

	repository, err := s.getTargetRepo(p.Repository)
	if err != nil {
		return nil, nil, err
	}
	value, err := pkgoci.GetFileSHA256(ctx, repository, p.Tag, p.File)
	if err != nil {
		return nil, nil, err
	}

	res = &oci.DownloadFileSha256Result{
		Length:             int64(len(value)),
		ContentDisposition: `attachment; filename*=UTF-8''` + url.QueryEscape(p.File) + ".sha256",
	}
	return res, io.NopCloser(strings.NewReader(value)), nil
}
