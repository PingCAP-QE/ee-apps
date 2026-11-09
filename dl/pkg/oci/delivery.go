package oci

import (
	"context"
	"encoding/json"
	"errors"
	"io"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry/remote"
)

// ResolveDeliveryFile resolves the manifest once and requires an unambiguous,
// exact title match. Neither descriptor URLs nor inline data are delivery targets.
func ResolveDeliveryFile(ctx context.Context, target oras.ReadOnlyTarget, ref, filename string) (ocispec.Descriptor, ocispec.Descriptor, error) {
	manifestDesc, data, err := oras.FetchBytes(ctx, target, ref, oras.DefaultFetchBytesOptions)
	if err != nil {
		return ocispec.Descriptor{}, ocispec.Descriptor{}, err
	}
	if manifestDesc.MediaType != ocispec.MediaTypeImageManifest {
		return ocispec.Descriptor{}, ocispec.Descriptor{}, errors.New("delivery requires an OCI image manifest")
	}
	var manifest ocispec.Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return ocispec.Descriptor{}, ocispec.Descriptor{}, err
	}
	var selected ocispec.Descriptor
	matches := 0
	for _, layer := range manifest.Layers {
		if layer.Annotations[AnnotationKeyFileName] == filename {
			selected = layer
			matches++
		}
	}
	if matches != 1 {
		return ocispec.Descriptor{}, ocispec.Descriptor{}, errors.New("delivery file must match exactly one layer")
	}
	if err := ValidateDeliveryDescriptor(selected); err != nil {
		return ocispec.Descriptor{}, ocispec.Descriptor{}, err
	}
	return manifestDesc, selected, nil
}

func ValidateDeliveryDescriptor(desc ocispec.Descriptor) error {
	if err := desc.Digest.Validate(); err != nil {
		return errors.New("invalid delivery digest")
	}
	if !desc.Digest.Algorithm().Available() || desc.Size < 0 || desc.MediaType == "" || len(desc.URLs) != 0 || len(desc.Data) != 0 {
		return errors.New("invalid delivery descriptor")
	}
	return nil
}

// FetchAuthorizedBlob never resolves a tag or follows descriptor URLs. The
// reader verifies size and digest while streaming with bounded memory. A late
// verification failure must abort the HTTP stream, and cannot retract sent bytes.
func FetchAuthorizedBlob(ctx context.Context, repo *remote.Repository, desc ocispec.Descriptor) (io.ReadCloser, error) {
	if err := ValidateDeliveryDescriptor(desc); err != nil {
		return nil, err
	}
	rc, err := repo.Blobs().Fetch(ctx, desc)
	if err != nil {
		return nil, err
	}
	return &verifiedBlob{VerifyReader: content.NewVerifyReader(rc, desc), closer: rc}, nil
}

type verifiedBlob struct {
	*content.VerifyReader
	closer io.Closer
}

func (b *verifiedBlob) Read(p []byte) (int, error) {
	n, err := b.VerifyReader.Read(p)
	if err == io.EOF {
		err = b.Verify()
		if err == nil {
			err = io.EOF
		}
	}
	return n, err
}

func (b *verifiedBlob) Close() error { return b.closer.Close() }
