package impl

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func TestValidateBuilderImage(t *testing.T) {
	configured := mustBuilderImageAllow(t, "ghcr.io/pingcap-qe", "hub.pingcap.net/pingcap/", "us-docker.pkg.dev/pingcap-testing-account")
	empty := config.BuilderImageAllowlist{}

	t.Run("an empty image keeps meaning the artifacts config decides", func(t *testing.T) {
		require.NoError(t, validateBuilderImage(empty, ""))
		require.NoError(t, validateBuilderImage(configured, ""))
	})

	t.Run("the value must always be a valid image reference", func(t *testing.T) {
		// Even without an allowlist the value has to be a valid image URL,
		// because it becomes the `image:` of a build step.
		for _, image := range []string{
			"ghcr.io/pingcap-qe/cd/utils/release:v1\nmalicious",
			"ghcr.io/pingcap-qe/cd/utils/release:v1 --entrypoint=/bin/sh",
			"ghcr.io/pingcap-qe/cd/utils/release:v1; rm -rf /",
			" ghcr.io/pingcap-qe/cd/utils/release:v1",
			"-ghcr.io/pingcap-qe/cd/utils/release:v1",
			"ghcr.io/pingcap-qe/",
			"GHCR.IO/PINGCAP-QE/cd/utils/release:v1",
			"ghcr.io/pingcap-qe/x@sha256:short",
		} {
			for _, allowed := range []config.BuilderImageAllowlist{empty, configured} {
				err := validateBuilderImage(allowed, image)
				require.Error(t, err, "image %q should be rejected", image)
				var badRequest *devbuild.DevBuildBadRequestError
				require.ErrorAs(t, err, &badRequest)
				require.Equal(t, 400, badRequest.Code)
			}
		}
	})

	t.Run("unconfigured allowlist accepts any valid reference", func(t *testing.T) {
		// Backward compatibility: without a configured allowlist the request
		// value is used as given.
		for _, image := range []string{
			"docker.io/library/golang:1.24.2-bullseye",
			"public.ecr.aws/amazonlinux/amazonlinux:2023.5.20240805.0",
			"example.com/anything:v1",
			"golang:1.24.2",
		} {
			require.NoError(t, validateBuilderImage(empty, image), "image %q should be accepted", image)
			require.NoError(t, validateBuilderImage(config.BuilderImageAllowlist{}, image), "image %q should be accepted", image)
		}
	})

	t.Run("configured allowlist matches the repository and ignores the tag", func(t *testing.T) {
		for _, image := range []string{
			"ghcr.io/pingcap-qe/cd/utils/release:v2025.10.12-7-gfdd779c",
			"ghcr.io/pingcap-qe/cd/builders/tidb:v2026.9.20-7-g5292897-centos7-go1.25",
			// Any tag or digest of an allowed repository passes: the tag is
			// not part of the policy.
			"ghcr.io/pingcap-qe/cd/utils/release",
			"ghcr.io/pingcap-qe/cd/utils/release:anything",
			"ghcr.io/pingcap-qe/cd/utils/release@sha256:0a8970b3866031092bc9dcf5ff14e0101c6cc204b624ca4cddaafdda31807985",
			// The configured entry may carry a trailing slash.
			"hub.pingcap.net/pingcap/pingcap-base:v1",
			"us-docker.pkg.dev/pingcap-testing-account/ghcr-remote/tektoncd/pipeline/sidecarlogresults@sha256:4e2567d980cf1768423f51d053fdaeb2656ab8a07fad769ce3311f8e20f1ecab",
		} {
			require.NoError(t, validateBuilderImage(configured, image), "image %q should be accepted", image)
		}
	})

	t.Run("configured allowlist rejects other repositories", func(t *testing.T) {
		for _, image := range []string{
			// Upstream images are legitimate inputs for the artifacts config,
			// but a caller may not point the build at them.
			"docker.io/library/golang:1.24.2-bullseye",
			"docker.io/rustlang/rust:nightly-2026-08-22",
			"public.ecr.aws/amazonlinux/amazonlinux:2023.5.20240805.0",
			"golang:1.24.2",
			// A different registry that merely spells a company namespace in its path.
			"evil.io/ghcr.io/pingcap-qe/cd/utils/release:v1",
			"docker.io/pingcap-qe/cd/utils/release:v1",
			// A repository that only shares a prefix with an allowed one.
			"ghcr.io/pingcap-qe-evil/x:v1",
		} {
			err := validateBuilderImage(configured, image)
			require.Error(t, err, "image %q should be rejected", image)
			var forbidden *devbuild.DevBuildForbiddenError
			require.ErrorAs(t, err, &forbidden)
			require.Equal(t, http.StatusForbidden, forbidden.Code)
		}

		// The rejection names the value, never the allowlist. Checked with a
		// value that does not itself contain a configured entry.
		err := validateBuilderImage(configured, "docker.io/library/golang:1.24.2-bullseye")
		var forbidden *devbuild.DevBuildForbiddenError
		require.ErrorAs(t, err, &forbidden)
		require.NotContains(t, forbidden.Message, "ghcr.io/pingcap-qe",
			"the message must not echo the allowlist")
		require.NotContains(t, forbidden.Message, "us-docker.pkg.dev/pingcap-testing-account")
	})
}

// mustBuilderImageAllow builds an allowlist for the table tests and fails when
// an entry is invalid, which is what the config loader guarantees.
func mustBuilderImageAllow(t *testing.T, entries ...string) config.BuilderImageAllowlist {
	t.Helper()
	allow, err := config.NewBuilderImageAllowlist(entries)
	require.NoError(t, err)
	return allow
}
