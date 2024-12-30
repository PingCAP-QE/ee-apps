package impl

import (
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const nightlyVerSuffix = "-nightly"

var (
	pkgNameRegex           = regexp.MustCompile(`^(.+)-v\d+\.\d+\.\d+`)
	pkgVersionNightlyRegex = regexp.MustCompile(`(-\d+-g[0-9a-f]{7,})$`)
	ociGATagRegex          = regexp.MustCompile(`^(v\d+\.\d+\.\d+)(-\w+-)?_(linux|darwin)_(amd64|arm64)$`)
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
func postCheckTiupPkg(localFile, remoteFileURL string) error {
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
func analyzeTiupFromOciArtifact(repo, tag string) ([]PublishRequestTiUP, error) {
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
	version := transformTiupVer(config["org.opencontainers.image.version"].(string), tag)

	// 3. Loop through TiUP packages
	var publishRequests []PublishRequestTiUP
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
		publishInfo := PublishInfoTiUP{
			Name:        tiupPkgName(file),
			Version:     version,
			OS:          os,
			Arch:        arch,
			Description: pkgMap["description"].(string),
			EntryPoint:  pkgMap["entrypoint"].(string),
		}
		publishRequests = append(publishRequests, PublishRequestTiUP{
			From:    from,
			Publish: publishInfo,
		})
	}

	return publishRequests, nil
}

func analyzeTiupFromOciArtifactUrl(url string) ([]PublishRequestTiUP, error) {
	repo, tag, err := splitRepoAndTag(url)
	if err != nil {
		return nil, err
	}
	return analyzeTiupFromOciArtifact(repo, tag)
}

// Get tiup pkg name from tarball filename
func tiupPkgName(tarballPath string) string {
	matches := pkgNameRegex.FindStringSubmatch(filepath.Base(tarballPath))
	if len(matches) > 1 {
		return matches[1]
	}
	return ""
}

func transformTiupVer(version, tag string) string {
	switch {
	case ociGATagRegex.MatchString(tag): // GA case
		// ociGATagRegex          = regexp.MustCompile(`^(v\d+\.\d+\.\d+)(-\w+)?_(linux|darwin)_(amd64|arm64)$`)
		matches := ociGATagRegex.FindStringSubmatch(tag)
		return strings.Join(matches[1:2], "")
	case ociNightlyTagRegex.MatchString(tag): // Nightly case
		// we replace the suffix part of version: '-[0-9]+-g[0-9a-f]+$' to "-nightly"
		return pkgVersionNightlyRegex.ReplaceAllString(version, "") + nightlyVerSuffix
	default:
		return version
	}
}

func isNightlyTiup(p PublishInfoTiUP) bool {
	return tiupVersionRegex.MatchString(p.Version)
}
