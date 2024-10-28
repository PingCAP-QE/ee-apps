package tiup

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/retry"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

const nightlyVerSuffix = "-nightly"

var (
	pkgNameRegex           = regexp.MustCompile(`^(.+)-v\d+\.\d+\.\d+`)
	pkgVersionNightlyRegex = regexp.MustCompile(`(-\d+-g[0-9a-f]{7,})$`)
	ociGATagRegex          = regexp.MustCompile(`^(v\d+\.\d+\.\d+)_(linux|darwin)_(amd64|arm64)$`)
	ociNightlyTagRegex     = regexp.MustCompile(`^(master|main)_(linux|darwin)_(amd64|arm64)$`)
	tiupVersionRegex       = regexp.MustCompile(`^v\d+\.\d+\.\d+.*(-nightly)$`)
)

// # GA case:
// 	#   when
// 	#   - the version is "vX.Y.Z-pre" and
// 	#   - the artifact_url has suffix: "vX.Y.Z_(linux|darwin)_(amd64|arm64)",
// 	#   then
// 	#   - set the version to "vX.Y.Z"

// check the remote file after published.
func postCheck(localFile, remoteFileURL string) error {
	// 1. Calculate the sha256sum of the local file
	localSum, err := calculateSHA256(localFile)
	if err != nil {
		return fmt.Errorf("failed to calculate local file sha256: %v", err)
	}

	// 2. Download the remote file
	tempFile, err := downloadHTTPFile(remoteFileURL)
	if err != nil {
		return fmt.Errorf("failed to download remote file: %v", err)
	}
	defer os.Remove(tempFile)

	// 3. Calculate the sha256sum of the remote file
	remoteSum, err := calculateSHA256(tempFile)
	if err != nil {
		return fmt.Errorf("failed to calculate remote file sha256: %v", err)
	}

	// 4. Compare the two sha256sums
	if localSum != remoteSum {
		return fmt.Errorf("sha256 mismatch: local %s, remote %s", localSum, remoteSum)
	}

	return nil
}

func calculateSHA256(filePath string) (string, error) {
	f, err := os.Open(filePath)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}

	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

func downloadFile(data *PublishRequest) (string, error) {
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

func downloadHTTPFile(url string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	tempFile, err := os.CreateTemp("", "remote_file_*")
	if err != nil {
		return "", err
	}
	defer tempFile.Close()

	_, err = io.Copy(tempFile, resp.Body)
	if err != nil {
		return "", err
	}

	return tempFile.Name(), nil
}

func downloadOCIFile(from *FromOci) (string, error) {
	repo, err := getOciRepo(from.Repo)
	if err != nil {
		return "", err
	}
	rc, _, err := oci.NewFileReadCloser(context.Background(), repo, from.Tag, from.File)
	if err != nil {
		return "", err
	}
	defer rc.Close()

	tempFile, err := os.CreateTemp("", "oci_download_*.tar.gz")
	if err != nil {
		return "", err
	}
	defer tempFile.Close()

	if _, err := io.Copy(tempFile, rc); err != nil {
		return "", err
	}

	return tempFile.Name(), nil
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

// Anlyze the artifact config and return the publish requests.
//
//		Steps:
//	 1. fetch the artifact config like "oras manifest fetch-config $repo:$tag" command, but we just use go code.
//	 2. judge if the key "net.pingcap.tibuild.tiup" existed in the result of previous result. If not we stop and return empty.
//	 3. loop for every element of the values of "net.pingcap.tibuild.tiup".
//	    3.1 set the publish `from` part:
//	    3.1.1 set the publish from type as "oci"
//	    3.1.2 set the publish from repo and tag from param `repo`, `tag`.
//	    3.1.3 set the publish from file with value of "file" key in the element.
//	    3.2. set the publish info
//	    3.2.1 set the publish info version with value of "org.opencontainers.image.version" key in top config.
//	    3.2.2 set the publish info os with value of "net.pingcap.tibuild.os" key in top config.
//	    3.2.3 set the publish info arch with value of "net.pingcap.tibuild.architecture" key in top config.
//	    3.2.4 set the publish info name with prefix part of the value of "file" key in the element, right trim from the "-vX.Y.Z" part.
//	    3.2.5 set the publish info description, entrypoint with value of same key in the element.
func AnalyzeFromOciArtifact(repo, tag string) ([]PublishRequest, error) {
	// 1. Fetch the artifact config
	config, ociDigest, err := fetchOCIArtifactConfig(repo, tag)
	if err != nil {
		return nil, err
	}

	// 2. Check if "net.pingcap.tibuild.tiup" exists
	tiupPackages, ok := config["net.pingcap.tibuild.tiup"].([]interface{})
	if !ok || len(tiupPackages) == 0 {
		return nil, nil // No TiUP packages to publish
	}

	// Get common information
	os := config["net.pingcap.tibuild.os"].(string)
	arch := config["net.pingcap.tibuild.architecture"].(string)
	version := transformVer(config["org.opencontainers.image.version"].(string), tag)

	// 3. Loop through TiUP packages
	var publishRequests []PublishRequest
	for _, pkg := range tiupPackages {
		pkgMap := pkg.(map[string]interface{})
		file := pkgMap["file"].(string)

		// 3.1 Set the publish 'from' part
		from := From{
			Type: FromTypeOci,
			Oci: &FromOci{
				Repo: repo,
				File: file,
				// use digest to avoid the problem of new override on the tag.
				Tag: ociDigest,
			},
		}

		// 3.2 Set the publish info
		publishInfo := PublishInfo{
			Name:        pkgName(file),
			Version:     version,
			OS:          os,
			Arch:        arch,
			Description: pkgMap["description"].(string),
			EntryPoint:  pkgMap["entrypoint"].(string),
		}
		publishRequests = append(publishRequests, PublishRequest{
			From:    from,
			Publish: publishInfo,
		})
	}

	return publishRequests, nil
}

func analyzeFromOciArtifactUrl(url string) ([]PublishRequest, error) {
	repo, tag, err := splitRepoAndTag(url)
	if err != nil {
		return nil, err
	}
	return AnalyzeFromOciArtifact(repo, tag)
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

// Get tiup pkg name from tarball filename
func pkgName(tarballPath string) string {
	matches := pkgNameRegex.FindStringSubmatch(filepath.Base(tarballPath))
	if len(matches) > 1 {
		return matches[1]
	}
	return ""
}

func transformVer(version, tag string) string {
	switch {
	case ociGATagRegex.MatchString(tag): // GA case
		return strings.TrimSuffix(version, "-pre")
	case ociNightlyTagRegex.MatchString(tag): // Nightly case
		// we replace the suffix part of version: '-[0-9]+-g[0-9a-f]+$' to "-nightly"
		return pkgVersionNightlyRegex.ReplaceAllString(version, nightlyVerSuffix)
	default:
		return version
	}
}
