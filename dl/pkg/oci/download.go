package oci

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	oras "oras.land/oras-go/v2"
	"oras.land/oras-go/v2/registry/remote"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

const AnnotationKeyFileName = "org.opencontainers.image.title"

func ListFiles(ctx context.Context, repo, tag string) ([]string, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return nil, err
	}

	// Note: The below code can be omitted if authentication is not required
	// reg := strings.SplitN(repo, "/", 2)[0]
	// repository.Client = &auth.Client{
	// 	Client: retry.DefaultClient,
	// 	Cache:  auth.DefaultCache,
	// 	Credential: auth.StaticCredential(reg, auth.Credential{
	// 		Username: "username",
	// 		Password: "password",
	// 	}),
	// }

	layers, err := listArtifactLayers(ctx, repository, tag)
	if err != nil {
		return nil, err
	}

	var ret []string
	for _, l := range layers {
		ret = append(ret, l.Annotations[AnnotationKeyFileName])
	}

	return ret, nil
}

func NewFileReadCloser(ctx context.Context, repo, tag, filename string) (io.ReadCloser, int64, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return nil, 0, err
	}

	// 1. get desired file descriptor in the artifact.
	// destination := strings.Join([]string{repo, tag}, ":")
	desiredFileDescriptor, err := fetchFileDescriptor(ctx, repository, tag, filename)
	if err != nil {
		return nil, 0, err
	}

	// 2. Fetch the blob of the desired file
	// blobRef := strings.Join([]string{repo, desiredFileDescriptor.Digest.String()}, "@")
	rc, err := repository.Blobs().Fetch(ctx, *desiredFileDescriptor)
	if err != nil {
		return nil, 0, err
	}

	return rc, desiredFileDescriptor.Size, nil
}

func GetFileSHA256(ctx context.Context, repo, tag, filename string) (string, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return "", err
	}

	// 1. get desired file descriptor in the artifact.
	// destination := strings.Join([]string{repo, tag}, ":")
	desiredFileDescriptor, err := fetchFileDescriptor(ctx, repository, tag, filename)
	if err != nil {
		return "", err
	}

	return desiredFileDescriptor.Digest.Encoded(), nil
}

func listArtifactLayers(ctx context.Context, target oras.ReadOnlyTarget, ref string) ([]ocispec.Descriptor, error) {
	// fetch manifest manifestBytes
	_, manifestBytes, err := oras.FetchBytes(ctx, target, ref, oras.DefaultFetchBytesOptions)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch the content of %q: %w", ref, err)
	}
	var manifest ocispec.Manifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		return nil, err
	}

	return manifest.Layers, nil
}

func fetchFileDescriptor(ctx context.Context, target oras.ReadOnlyTarget, ref, filename string) (*ocispec.Descriptor, error) {
	layers, err := listArtifactLayers(ctx, target, ref)
	if err != nil {
		return nil, err
	}

	// Find the desired file and return the descriptor.
	for _, f := range layers {
		if f.Annotations[AnnotationKeyFileName] == filename {
			return &f, nil
		}
	}

	return nil, fmt.Errorf("not found file: %s", filename)
}
