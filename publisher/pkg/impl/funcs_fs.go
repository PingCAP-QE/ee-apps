package impl

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
	"golang.org/x/mod/semver"
)

var reTagOsArchSuffix = regexp.MustCompile(`_((linux|darwin)_(amd64|arm64))$`)

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
func analyzeFsFromOciArtifact(repo, tag string) ([]PublishRequest, error) {
	switch {
	case strings.HasSuffix(repo, "/pingcap/tidb-tools/package"):
		return analyzeFsFromOciArtifactForTiDBTools(repo, tag)
	case strings.HasSuffix(repo, "/pingcap/tidb-binlog/package"):
		return analyzeFsFromOciArtifactForTiDBBinlog(repo, tag)
		// more special cases can be added here.
	}

	// 1. Fetch the artifact config
	config, ociDigest, err := fetchOCIArtifactConfig(repo, tag)
	if err != nil {
		return nil, err
	}

	// 2. Check if "net.pingcap.tibuild.tiup" exists
	tiupPackages, ok := config["net.pingcap.tibuild.tiup"].([]interface{})
	if !ok || len(tiupPackages) == 0 {
		return nil, nil // No fileserver packages to publish
	}

	// Get common information
	version := transformFsVer(config["net.pingcap.tibuild.git-sha"].(string), tag)

	// 3. Loop through TiUP packages
	// TODO: set the prefix from the match group part with reTagOsArchSuffix from the `tag`:
	entryPointPrefix := getFsEntryPointPrefix(tag)
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
			Name:    tiupPkgName(file),
			Version: version,
			// TODO: if the pkgName is "tidb",  then the entry point should be "tidb-server.tar.gz"
			EntryPoint: transformFsEntryPoint(entryPointPrefix, file),
		}
		publishRequests = append(publishRequests, PublishRequest{
			From:    from,
			Publish: publishInfo,
		})
	}

	return publishRequests, nil
}

func analyzeFsFromOciArtifactForTiDBTools(repo, tag string) ([]PublishRequest, error) {
	// 1. Fetch the artifact config
	config, ociDigest, err := fetchOCIArtifactConfig(repo, tag)
	if err != nil {
		return nil, err
	}

	// Get common information
	version := transformFsVer(config["net.pingcap.tibuild.git-sha"].(string), tag)

	// Find the file.
	repository, err := getOciRepo(repo)
	if err != nil {
		return nil, fmt.Errorf("failed to get OCI repository: %v", err)
	}

	files, err := oci.ListFiles(context.Background(), repository, tag)
	if err != nil {
		return nil, fmt.Errorf("failed to get file list: %v", err)
	}
	var file string
	for _, f := range files {
		if strings.HasSuffix(f, ".tar.gz") && strings.HasPrefix(f, "tidb-tools-") {
			file = f
			break
		}
	}

	// 3.1 Set the publish 'from' part
	from := From{
		Type: FromTypeOci,
		Oci: &FromOci{
			Repo: repo,
			File: file,
			Tag:  ociDigest,
		},
	}

	// 3.2 Set the publish info
	publishInfo := PublishInfo{
		Name:       "tidb-tools",
		Version:    version,
		EntryPoint: fmt.Sprintf("%s/tidb-tools.tar.gz", getFsEntryPointPrefix(tag)),
	}
	return []PublishRequest{{From: from, Publish: publishInfo}}, nil
}

func analyzeFsFromOciArtifactForTiDBBinlog(repo, tag string) ([]PublishRequest, error) {
	// 1. Fetch the artifact config
	config, ociDigest, err := fetchOCIArtifactConfig(repo, tag)
	if err != nil {
		return nil, err
	}

	// Get common information
	version := transformFsVer(config["net.pingcap.tibuild.git-sha"].(string), tag)

	// Find the file.
	repository, err := getOciRepo(repo)
	if err != nil {
		return nil, fmt.Errorf("failed to get OCI repository: %v", err)
	}

	files, err := oci.ListFiles(context.Background(), repository, tag)
	if err != nil {
		return nil, fmt.Errorf("failed to get file list: %v", err)
	}
	var file string
	for _, f := range files {
		if strings.HasSuffix(f, ".tar.gz") && strings.HasPrefix(f, "binaries-") {
			file = f
			break
		}
	}

	// 3.1 Set the publish 'from' part
	from := From{
		Type: FromTypeOci,
		Oci: &FromOci{
			Repo: repo,
			File: file,
			Tag:  ociDigest,
		},
	}

	// 3.2 Set the publish info
	publishInfo := PublishInfo{
		Name:       "tidb-binlog",
		Version:    version,
		EntryPoint: fmt.Sprintf("%s/tidb-binlog.tar.gz", getFsEntryPointPrefix(tag)),
	}
	return []PublishRequest{{From: from, Publish: publishInfo}}, nil
}

func analyzeFsFromOciArtifactUrl(url string) ([]PublishRequest, error) {
	repo, tag, err := splitRepoAndTag(url)
	if err != nil {
		return nil, err
	}
	return analyzeFsFromOciArtifact(repo, tag)
}

func transformFsVer(commitSHA1, tag string) string {
	// Remove OS and arch suffix using regex
	branch := reTagOsArchSuffix.ReplaceAllString(tag, "")
	return fmt.Sprintf("%s#%s", branch, commitSHA1)
}

func transformFsEntryPoint(prefix, file string) string {
	base := tiupPkgName(file)
	switch base {
	case "tidb", "pd", "tikv":
		return fmt.Sprintf("%s/%s-server.tar.gz", prefix, base)
	default:
		return fmt.Sprintf("%s/%s.tar.gz", prefix, base)
	}
}

func getFsEntryPointPrefix(tag string) string {
	if match := reTagOsArchSuffix.FindStringSubmatch(tag); len(match) > 1 {
		return match[1]
	}

	return "centos7"
}

func targetFsFullPaths(p *PublishInfo) []string {
	var ret []string

	// the <branch>/<commit> path: pingcap/<comp>/<branch>/<commit>/<entrypoint>
	ret = append(ret, filepath.Join("download/builds/pingcap", p.Name, strings.ReplaceAll(p.Version, "#", "/"), p.EntryPoint))
	// if the part before the '#' char in p.Version is semver git tag format, then we need only one path.
	if semver.IsValid(strings.Split(p.Version, "#")[0]) {
		return ret
	}

	// the <branch>/<commit> path: pingcap/<comp>/<commit>/<entrypoint>
	ret = append(ret, filepath.Join("download/builds/pingcap", p.Name, filepath.Base(strings.ReplaceAll(p.Version, "#", "/")), p.EntryPoint))

	return ret
}

func targetFsRefKeyValue(p *PublishInfo) [2]string {
	var ret [2]string
	verParts := strings.Split(p.Version, "#")
	if len(verParts) > 1 {
		ret[0] = fmt.Sprintf("download/refs/pingcap/%s/%s/sha1", p.Name, verParts[0])
		ret[1] = verParts[1]
	} else {
		ret[0] = fmt.Sprintf("download/refs/pingcap/%s/%s/sha1", p.Name, "master")
		ret[1] = verParts[0]
	}

	return ret
}
