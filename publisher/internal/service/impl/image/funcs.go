package image

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/crane"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/rs/zerolog"
)

// getManifestEntries gets manifest and digest for each arch tag, returning v1.Descriptor with Platform info.
func getManifestEntries(ctx context.Context, repo string, archTags []string, logger zerolog.Logger) ([]v1.Descriptor, error) {
	manifests := []v1.Descriptor{}
	for _, archTag := range archTags {
		ref := fmt.Sprintf("%s:%s", repo, archTag)
		desc, err := crane.Manifest(ref, crane.WithContext(ctx))
		if err != nil {
			logger.Error().Err(err).Str("ref", ref).Msg("Failed to get manifest")
			continue
		}

		// Compose v1.Descriptor with digest and platform
		var descriptor v1.Descriptor
		if err := json.Unmarshal(desc, &descriptor); err != nil {
			logger.Error().Err(err).Str("ref", ref).Msg("Failed to unmarshal manifest")
			continue
		}
		manifests = append(manifests, descriptor)
	}
	return manifests, nil
}

// listSingleArchTags lists all tags in the given repo using crane.
// Currently, it only supports two os/architectures: linux/amd64 and linux/arm64.
func listSingleArchTags(repo, oneArchTag string) []string {
	// if `oneArchTag` is ends with '_linux_amd64', then another one will end with '_linux_arm64'
	// if `oneArchTag` is ends with '_linux_arm64', then another one will end with '_linux_amd64'
	var anotherArchTag string
	if strings.HasSuffix(oneArchTag, "_linux_amd64") {
		anotherArchTag = strings.Replace(oneArchTag, "_amd64", "_arm64", 1)
	} else if strings.HasSuffix(oneArchTag, "_linux_arm64") {
		anotherArchTag = strings.Replace(oneArchTag, "_arm64", "_amd64", 1)
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

// parseRepoAndTag parses the repo and tag from an image URL of the form repo:tag.
func parseRepoAndTag(imageURL string) (repo, tag string, err error) {
	parts := strings.Split(imageURL, ":")
	if len(parts) != 2 {
		return "", "", fmt.Errorf("invalid image_url, must be in form repo:tag")
	}
	return parts[0], parts[1], nil
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

func pushMultiarchManifest(repo string, tags []string, manifests []v1.Descriptor, logger zerolog.Logger) error {
	_, err := name.NewRepository(repo)
	if err != nil {
		return fmt.Errorf("invalid repo name: %w", err)
	}

	// Start with an empty index
	idx := mutate.IndexMediaType(empty.Index, types.OCIImageIndex)
	// Append each manifest descriptor to the index
	for _, desc := range manifests {
		idx = mutate.AppendManifests(idx, mutate.IndexAddendum{
			Descriptor: desc,
		})
	}

	// Push for each tag
	for _, tag := range tags {
		tagRef, err := name.NewTag(fmt.Sprintf("%s:%s", repo, tag))
		if err != nil {
			logger.Error().Err(err).Str("tag", tag).Msg("invalid tag")
			continue
		}
		logger.Info().Str("tag", tag).Msg("Pushing multi-arch manifest")
		if err := remote.WriteIndex(tagRef, idx, remote.WithAuthFromKeychain(authn.DefaultKeychain)); err != nil {
			logger.Error().Err(err).Str("tag", tag).Msg("failed to push manifest list")
			return err
		}
	}

	return nil
}
