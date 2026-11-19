package share

import (
	"context"
	"io"

	pkgoci "github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
	"oras.land/oras-go/v2/registry/remote"
)

type RepoProvider interface {
	GetTargetRepo(string) (*remote.Repository, error)
}

type OCIBackend struct{ Provider RepoProvider }

func (b OCIBackend) Resolve(ctx context.Context, repository, tag, file string) (Artifact, error) {
	repo, err := b.Provider.GetTargetRepo(repository)
	if err != nil {
		return Artifact{}, err
	}
	manifest, blob, err := pkgoci.ResolveDeliveryFile(ctx, repo, tag, file)
	if err != nil {
		return Artifact{}, err
	}
	return Artifact{Repository: repository, SourceTag: tag, ManifestDigest: manifest.Digest.String(), BlobDigest: blob.Digest.String(), BlobSize: blob.Size, MediaType: blob.MediaType, Filename: file}, nil
}

func (b OCIBackend) Fetch(ctx context.Context, a Artifact) (io.ReadCloser, error) {
	repo, err := b.Provider.GetTargetRepo(a.Repository)
	if err != nil {
		return nil, err
	}
	return pkgoci.FetchAuthorizedBlob(ctx, repo, a.Descriptor())
}
