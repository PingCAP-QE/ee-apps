package image

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/partial"
	"github.com/google/go-containerregistry/pkg/v1/remote"
)

// listSingleArchTags lists all tags in the given repo using crane.
// Currently, it only supports two os/architectures: linux/amd64 and linux/arm64.
func listSingleArchTags(repo, oneArchTag string) []string {
	// if `oneArchTag` is ends with '_linux_amd64', then another one will end with '_linux_arm64'
	// if `oneArchTag` is ends with '_linux_arm64', then another one will end with '_linux_amd64'
	var anotherArchTag string
	if b, ok := strings.CutSuffix(oneArchTag, "_linux_amd64"); ok {
		anotherArchTag = b + "_linux_arm64"
	}
	if b, ok := strings.CutSuffix(oneArchTag, "_linux_arm64"); ok {
		anotherArchTag = b + "_linux_amd64"
	}

	ret := []string{oneArchTag}
	// find if anotherArchTag exists
	if _, err := crane.Head(repo + ":" + anotherArchTag); err != nil {
		// not found other arch tags
		return ret
	}

	return append(ret, anotherArchTag)
}

// filterArchTags filters tags for arch-specific tags based on the baseTag.
func filterArchTags(baseTag string, allTags []string) []string {
	archTagPattern := fmt.Sprintf(`^%s[-_]linux[-_](amd64|arm64)$`, regexp.QuoteMeta(baseTag))
	archTagRegex := regexp.MustCompile(archTagPattern)
	archTags := []string{}
	for _, t := range allTags {
		if archTagRegex.MatchString(t) {
			archTags = append(archTags, t)
		}
	}
	return archTags
}

// computeBaseTags computes the base tags from the pushed tag and release tag suffix.
func computeBaseTags(pushedTag, releaseTagSuffix string) []string {
	tag := pushedTag
	tag = regexp.MustCompile(`[-_](amd64|arm64)$`).ReplaceAllString(tag, "")
	tag = regexp.MustCompile(`[-_]linux$`).ReplaceAllString(tag, "")

	tags := []string{tag}
	// Add tag with releaseTagSuffix removed
	releaseSuffixPattern := fmt.Sprintf(`[-_]%s$`, regexp.QuoteMeta(releaseTagSuffix))
	tagNoRelease := regexp.MustCompile(releaseSuffixPattern).ReplaceAllString(tag, "")
	if tagNoRelease != tag {
		tags = append(tags, tagNoRelease)
	}
	// If not semver, trim commit SHA part
	semverPattern := regexp.MustCompile(`^v?[0-9]+\.[0-9]+\.[0-9]+([-+][a-zA-Z0-9]+)*$`)
	if !semverPattern.MatchString(tag) {
		// Remove -<sha> at the end
		tagNoSHA := regexp.MustCompile(`-[0-9a-f]{7,40}$`).ReplaceAllString(tagNoRelease, "")
		if tagNoSHA != tagNoRelease {
			tags = append(tags, tagNoSHA)
		}
	}
	return tags
}

func pushMultiarchManifest(repo string, newTags, manifests []string, options []crane.Option) (string, error) {
	o := crane.GetOptions(options...)
	base := empty.Index
	adds := make([]mutate.IndexAddendum, 0, len(manifests))

	for _, m := range manifests {
		ref, err := name.ParseReference(m, o.Name...)
		if err != nil {
			return "", err
		}
		desc, err := remote.Get(ref, o.Remote...)
		if err != nil {
			return "", err
		}
		if desc.MediaType.IsImage() {
			img, err := desc.Image()
			if err != nil {
				return "", err
			}

			cf, err := img.ConfigFile()
			if err != nil {
				return "", err
			}
			newDesc, err := partial.Descriptor(img)
			if err != nil {
				return "", err
			}
			newDesc.Platform = cf.Platform()
			adds = append(adds, mutate.IndexAddendum{
				Add:        img,
				Descriptor: *newDesc,
			})
		} else if desc.MediaType.IsIndex() {
			idx, err := desc.ImageIndex()
			if err != nil {
				return "", err
			}

			adds = append(adds, mutate.IndexAddendum{
				Add: idx,
			})
		} else {
			return "", fmt.Errorf("saw unexpected MediaType %q for %q", desc.MediaType, m)
		}
	}

	idx := mutate.AppendManifests(base, adds...)

	for _, newTag := range newTags {
		tag := fmt.Sprintf("%s:%s", repo, newTag)
		ref, err := name.ParseReference(tag, o.Name...)
		if err != nil {
			return "", fmt.Errorf("parsing reference %s: %w", tag, err)
		}

		if err := remote.WriteIndex(ref, idx, o.Remote...); err != nil {
			return "", fmt.Errorf("pushing image %s: %w", tag, err)
		}
	}

	hash, err := idx.Digest()
	if err != nil {
		return "", fmt.Errorf("calculating index digest: %w", err)
	}
	return hash.String(), nil
}
