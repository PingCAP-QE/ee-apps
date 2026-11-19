package impl

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/distribution/reference"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// validateBuilderImage validates a caller-supplied builder image.
//
// The value is always parsed as an image reference, so a devbuild can never
// carry something that is not a valid image URL (the value ends up as the
// `image:` of the Tekton build step, which runs with CI credentials and pushes
// to the testing-account registry).
//
// Then the repository part must be inside one of the namespaces listed in
// devbuild.builder_image_allowed_repositories. Only the repository is matched:
// tags and digests are not restricted. The policy is fail-closed: an empty
// allowlist rejects every override, because treating "not configured" as
// "allow anything" would let a missing config key hand the build step an
// arbitrary image. An empty image is still accepted: it means "use the image
// the artifacts config pins for the component", which never reaches this
// allowlist.
//
// The policy belongs in this API, because this is where the request enters the
// system. The Tekton tasks downstream cannot do it: they receive the pinned
// image and the override through the same param, so they cannot tell the two
// apart, and a value list there would have to be updated for every image the
// artifacts config pins.
func validateBuilderImage(allowedRepositories []string, image string) error {
	if image == "" {
		return nil
	}
	named, err := reference.ParseNormalizedNamed(image)
	if err != nil {
		return &devbuild.DevBuildBadRequestError{
			Code:    http.StatusBadRequest,
			Message: fmt.Sprintf("builderImg is not a valid image reference: %v", err),
		}
	}

	if len(allowedRepositories) == 0 {
		return &devbuild.DevBuildForbiddenError{
			Code:    http.StatusForbidden,
			Message: "builderImg overrides are disabled: devbuild.builder_image_allowed_repositories is not configured",
		}
	}

	// The repository without the tag or digest, e.g.
	// "ghcr.io/pingcap-qe/cd/utils/release".
	repository := reference.TrimNamed(named).Name()
	allowed := make([]string, 0, len(allowedRepositories))
	for _, entry := range allowedRepositories {
		normalized := normalizeRepositoryPrefix(entry)
		if normalized == "" {
			continue
		}
		allowed = append(allowed, normalized)
		if repository == normalized || strings.HasPrefix(repository, normalized+"/") {
			return nil
		}
	}
	return &devbuild.DevBuildForbiddenError{
		Code:    http.StatusForbidden,
		Message: fmt.Sprintf("builderImg repository %q must be one of %s", repository, strings.Join(allowed, ", ")),
	}
}

// normalizeRepositoryPrefix makes a configured namespace comparable with the
// repository of a parsed reference: no trailing slash, lower case (repository
// names are lower case in the image reference grammar).
func normalizeRepositoryPrefix(entry string) string {
	return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(entry), "/"))
}
