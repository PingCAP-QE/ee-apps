package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/retry"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

func downloadFile(data *PublishRequestTiUP) (string, error) {
	switch data.From.Type {
	case FromTypeOci:
		// save to file with `saveTo` param:
		return downloadOCIFile(data.From.Oci)
	case FromTypeHTTP:
		return downloadHTTPFile(data.From.HTTP.URL)
	default:
		return "", fmt.Errorf("unknown from type: %v", data.From.Type)
	}
}

func downloadOCIFile(from *FromOci) (ret string, err error) {
	err = doWithOCIFile(from, func(input io.Reader) error {
		ret, err = downloadFileFromReader(input)
		return err
	})

	return
}

func downloadHTTPFile(url string) (ret string, err error) {
	err = doWithHttpFile(url, func(input io.Reader) error {
		ret, err = downloadFileFromReader(input)
		return err
	})
	return
}

func downloadFileFromReader(input io.Reader) (ret string, err error) {
	doWithTempFileFromReader(input, func(input *os.File) error {
		ret = input.Name()
		return nil
	})

	return
}

func doWithTempFileFromReader(input io.Reader, fn func(input *os.File) error) error {
	tempFile, err := os.CreateTemp("", "download_file_*")
	if err != nil {
		return err
	}
	defer tempFile.Close()
	if _, err := io.Copy(tempFile, input); err != nil {
		return err
	}

	return fn(tempFile)
}

func doWithHttpFile(url string, fn func(input io.Reader) error) error {
	resp, err := http.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	return fn(resp.Body)
}

func doWithOCIFile(from *FromOci, fn func(input io.Reader) error) error {
	repo, err := getOciRepo(from.Repo)
	if err != nil {
		return err
	}
	rc, _, err := oci.NewFileReadCloser(context.Background(), repo, from.Tag, from.File)
	if err != nil {
		return err
	}
	defer rc.Close()

	return fn(rc)
}

func getOciRepo(repo string) (*remote.Repository, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return nil, err
	}

	reg := strings.SplitN(repo, "/", 2)[0]
	repository.Client = &auth.Client{
		Client:     retry.DefaultClient,
		Cache:      auth.DefaultCache,
		Credential: auth.StaticCredential(reg, auth.EmptyCredential),
	}

	return repository, nil
}

func splitRepoAndTag(url string) (string, string, error) {
	splitor := ":"
	if strings.Contains(url, "@sha256:") {
		splitor = "@"
	}
	parts := strings.SplitN(url, splitor, 2)
	if len(parts) != 2 {
		return "", "", fmt.Errorf("invalid URL: %s", url)
	}

	return parts[0], parts[1], nil
}

// return the config of the artifact and the digest of the artifact.
func fetchOCIArtifactConfig(repo, tag string) (map[string]interface{}, string, error) {
	repository, err := getOciRepo(repo)
	if err != nil {
		return nil, "", fmt.Errorf("failed to get OCI repository: %v", err)
	}

	// Fetch the artifact manifest
	desc, manifestBytes, err := oras.FetchBytes(context.Background(), repository, tag, oras.DefaultFetchBytesOptions)
	if err != nil {
		return nil, "", fmt.Errorf("failed to fetch artifact manifest: %v", err)
	}

	var manifest ocispec.Manifest
	if err := json.Unmarshal(
		manifestBytes, &manifest); err != nil {
		return nil, "", fmt.Errorf("failed to unmarshal manifest: %v", err)
	}

	// Fetch the config content
	configBytes, err := content.FetchAll(context.Background(), repository, manifest.Config)
	if err != nil {
		return nil, "", fmt.Errorf("failed to fetch artifact config: %v", err)
	}
	var config map[string]interface{}
	if err := json.Unmarshal(configBytes, &config); err != nil {
		return nil, "", fmt.Errorf("failed to unmarshal config: %v", err)
	}

	return config, desc.Digest.String(), nil
}
