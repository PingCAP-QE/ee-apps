package dl

import (
	"context"
	"io"
	"log"
	"net/url"

	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	pkgoci "github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

// oci service example implementation.
// The example methods log the requests and return zero values.
type ocisrvc struct {
	logger *log.Logger
}

// NewOci returns the oci service implementation.
func NewOci(logger *log.Logger) oci.Service {
	return &ocisrvc{logger}
}

// ListFiles implements list-files.
func (s *ocisrvc) ListFiles(ctx context.Context, p *oci.ListFilesPayload) (res []string, err error) {
	s.logger.Print("oci.list-files")

	files, err := pkgoci.ListFiles(ctx, p.Repository, p.Tag)
	if err != nil {
		return nil, oci.MakeInvalidFilePath(err)
	}

	return files, nil
}

// DownloadFile implements download-file.
func (s *ocisrvc) DownloadFile(ctx context.Context, p *oci.DownloadFilePayload) (res *oci.DownloadFileResult, resp io.ReadCloser, err error) {
	s.logger.Print("oci.download-files")

	rc, length, err := pkgoci.NewFileReadCloser(ctx, p.Repository, p.Tag, p.File)
	if err != nil {
		return nil, nil, err
	}

	res = &oci.DownloadFileResult{
		Length:             length,
		ContentDisposition: `attachment; filename*=UTF-8''` + url.QueryEscape(p.File),
	}
	return res, rc, nil
}
