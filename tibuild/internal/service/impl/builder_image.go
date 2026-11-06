package impl

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/distribution/reference"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// validateBuilderImage validates a caller-supplied builder image.
//
// The value is always parsed as an image reference, so a devbuild can never
// carry something that is not a valid image URL (the value ends up as the
// `image:` of the Tekton build step, which runs with CI credentials and pushes
// to the testing-account registry).
//
// Then, if an allowlist is configured (devbuild.builder_image_allowed_repositories),
// the repository part must be inside one of the listed namespaces. Only the
// repository is matched: tags and digests are not restricted. An empty
// allowlist disables that policy, which keeps the previous behaviour, and an
// empty image is always accepted because it means "use the image the artifacts
// config pins for the component".
//
// The policy belongs in this API, because this is where the request enters the
// system. The Tekton tasks downstream cannot do it: they receive the pinned
// image and the override through the same param, so they cannot tell the two
// apart, and a value list there would have to be updated for every image the
// artifacts config pins.
func validateBuilderImage(allow config.BuilderImageAllowlist, image string) error {
	if image == "" {
		return nil
	}
	badRequest := func(message string) error {
		return &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: message}
	}
	named, err := reference.ParseNormalizedNamed(image)
	if err != nil {
		return badRequest(fmt.Sprintf("builderImg is not a valid image reference: %v", err))
	}
	if allow.Empty() {
		return nil
	}

	// The repository without the tag or digest, e.g.
	// "ghcr.io/pingcap-qe/cd/utils/release".
	repository := reference.TrimNamed(named).Name()
	if allow.Allows(repository) {
		return nil
	}
	return badRequest(fmt.Sprintf("builderImg repository %q must be one of %s",
		repository, strings.Join(allow.Entries(), ", ")))
}
