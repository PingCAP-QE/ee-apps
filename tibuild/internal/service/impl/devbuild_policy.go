package impl

import (
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// devbuildPolicy is an immutable snapshot of the configuration the devbuild
// request path reads. Reload builds a whole new snapshot and swaps the pointer,
// so a request observes one consistent revision: it can never combine the new
// allowlist with the old product map, nor read a slice header that is being
// replaced in place. Publish a snapshot once and treat it as read-only.
type devbuildPolicy struct {
	// repoAllowlist holds the explicit githubRepo policy. The zero value allows
	// nothing but the product default.
	repoAllowlist config.RepoAllowlist
	// builderImageAllowlist holds the repository prefixes a caller may name in
	// builderImg. Empty disables the check (legacy behaviour).
	builderImageAllowlist []string
	// productRepoMap maps a product name to its default GitHub repository.
	productRepoMap map[string]string
	// imageMirrorURLMap maps an image URL prefix to its mirror prefix.
	imageMirrorURLMap map[string]string
}

// newDevbuildPolicy copies the configuration into a new snapshot, so a caller
// that keeps mutating cfg cannot change a snapshot that is already published.
//
// When the githubRepo allowlist does not parse, the returned policy is still
// usable: its allowlist is empty, which rejects every explicit repository. The
// error is returned for the caller to log and decide what to publish.
func newDevbuildPolicy(cfg *config.Service) (*devbuildPolicy, error) {
	policy := &devbuildPolicy{
		builderImageAllowlist: append([]string(nil), cfg.DevBuild.BuilderImageAllowedRepositories...),
		productRepoMap:        copyStringMap(cfg.ProductRepoMap),
		imageMirrorURLMap:     copyStringMap(cfg.ImageMirrorURLMap),
	}
	allowlist, err := config.NewRepoAllowlist(cfg.DevBuild.GithubRepoAllowlist)
	if err != nil {
		return policy, err
	}
	policy.repoAllowlist = allowlist
	return policy, nil
}

// currentPolicy returns the snapshot the service is serving. A service built
// without one publishes an empty snapshot, which denies explicit repositories
// instead of allowing them by accident.
func (s *devbuildsrvc) currentPolicy() *devbuildPolicy {
	if policy := s.policy.Load(); policy != nil {
		return policy
	}
	return &devbuildPolicy{}
}

// copyStringMap returns a shallow copy of src, or nil when src is nil so that
// an unset map stays distinguishable from an empty one.
func copyStringMap(src map[string]string) map[string]string {
	if src == nil {
		return nil
	}
	dst := make(map[string]string, len(src))
	for key, value := range src {
		dst[key] = value
	}
	return dst
}
