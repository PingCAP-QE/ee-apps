package impl

import (
	"context"
	"fmt"
	"path"
	"regexp"
	"strings"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

var (
	reTagOsArchSuffix   = regexp.MustCompile(`_((linux|darwin)_(amd64|arm64))$`)
	reTarballNameSuffix = regexp.MustCompile(`(.+)-v\d+\.\d+\.\d+(-.+)?-(linux|darwin)-(amd64|arm64).tar.gz$`)
)

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
func analyzeFsFromOciArtifact(repo, tag string) (*PublishRequestFS, error) {
	// Find the file.
	repository, err := getOciRepo(repo)
	if err != nil {
		return nil, fmt.Errorf("failed to get OCI repository: %v", err)
	}

	files, err := oci.ListFiles(context.Background(), repository, tag)
	if err != nil {
		return nil, fmt.Errorf("failed to get file list: %v", err)
	}

	// 1. Fetch the artifact config
	config, ociDigest, err := fetchOCIArtifactConfig(repo, tag)
	if err != nil {
		return nil, err
	}

	from := From{
		Type: FromTypeOci,
		Oci: &FromOci{
			Repo: repo,
			// use digest to avoid the problem of new override on the tag.
			Tag: ociDigest,
		},
	}
	fm, err := getFileKeys(files)
	if err != nil {
		return nil, err
	}

	publishInfo := PublishInfoFS{
		Repo:            strings.TrimSuffix(strings.SplitN(repo, "/", 2)[1], "/package"),
		Branch:          reTagOsArchSuffix.ReplaceAllString(tag, ""),
		CommitSHA:       config["net.pingcap.tibuild.git-sha"].(string),
		FileTransferMap: fm,
	}

	return &PublishRequestFS{From: from, Publish: publishInfo}, nil
}

func getFileKeys(files []string) (map[string]string, error) {
	fm := map[string]string{}
	for _, f := range files {
		if reTarballNameSuffix.MatchString(f) {
			fm[f] = reTarballNameSuffix.ReplaceAllString(f, "")
			// how to extract the os and arch from the file name with regexp: `reTarballNameSuffix`
			// example: tidb-server-v1.1.1-linux-amd64.tar.gz => linux_amd64/tidb-server.tar.gz
			matches := reTarballNameSuffix.FindStringSubmatch(f)
			if len(matches) < 5 {
				return nil, fmt.Errorf("failed to match tarball name: %s", f)
			}
			baseName := matches[1]
			os := matches[3]
			arch := matches[4]
			fm[f] = fmt.Sprintf("%s_%s/%s.tar.gz", os, arch, baseName)
		}
	}
	return fm, nil
}

func analyzeFsFromOciArtifactUrl(url string) (*PublishRequestFS, error) {
	repo, tag, err := splitRepoAndTag(url)
	if err != nil {
		return nil, err
	}
	return analyzeFsFromOciArtifact(repo, tag)
}

func targetFsFullPaths(p *PublishInfoFS) map[string]string {
	keyPrefix := path.Join("download/builds", p.Repo, p.Branch, p.CommitSHA)
	ret := map[string]string{}
	for k, v := range p.FileTransferMap {
		ret[k] = path.Join(keyPrefix, v)
	}

	return ret
}

func targetFsRefKeyValue(p *PublishInfoFS) [2]string {
	key := path.Join("download/refs", p.Repo, p.Branch, "sha1")
	val := p.CommitSHA

	return [2]string{key, val}
}
