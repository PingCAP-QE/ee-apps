package impl

import (
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// currentAllowlist returns the repository allowlist of the configuration
// revision the service is serving. The pointer is replaced by Reload, so a
// request never needs a lock and never sees a partially built list.
func (s *devbuildsrvc) currentAllowlist() config.RepoAllowlist {
	if allow := s.repoAllowlist.Load(); allow != nil {
		return *allow
	}
	return config.RepoAllowlist{}
}

// checkExplicitRepo decides whether a caller-supplied repository may be used.
//
// A non-empty allowlist is the only judgement. An empty allowlist is still
// fail-closed: the only explicit value it accepts is the product default, which
// the caller may repeat redundantly. Unknown products have no default, so an
// explicit repository for them is rejected.
func checkExplicitRepo(explicitRepo string, productRepoMap map[string]string, product string, allow config.RepoAllowlist) error {
	repo := strings.TrimSpace(explicitRepo)

	allowed := false
	if !allow.Empty() {
		allowed = allow.Allows(repo)
	} else if productDefault := strings.TrimSpace(productRepoMap[product]); productDefault != "" {
		allowed = strings.EqualFold(repo, productDefault)
	}
	if allowed {
		return nil
	}

	// The message must never echo the allowlist: it tells a caller that the
	// value was rejected, not what would have been accepted.
	return &devbuild.DevBuildForbiddenError{
		Code:    http.StatusForbidden,
		Message: fmt.Sprintf("githubRepo '%s' is not in the devbuild allowlist", repo),
	}
}

// recordRepoDenied emits the rejection signal: one counter increment per
// surface and a warning with the fields needed to find the caller. Neither the
// token nor the allowlist content is logged.
func (s *devbuildsrvc) recordRepoDenied(surface, repo, product, createdBy string) {
	if s.repoMetrics != nil {
		s.repoMetrics.recordDenied(surface)
	}
	if s.logger != nil {
		s.logger.Warn().
			Str("surface", surface).
			Str("repo", repo).
			Str("product", product).
			Str("created_by", createdBy).
			Msg("devbuild githubRepo rejected by the allowlist")
	}
}

// isRepoDenied reports whether err is an allowlist rejection, so the callers
// can record it once at the point where they know the request context.
func isRepoDenied(err error) bool {
	var forbidden *devbuild.DevBuildForbiddenError
	return errors.As(err, &forbidden)
}

// devbuildRepoMetrics holds the devbuild policy counters. A nil registry keeps
// every method a no-op, which is what tests and the metrics-disabled API need.
type devbuildRepoMetrics struct {
	denied *prometheus.CounterVec
}

func newDevbuildRepoMetrics(registerer prometheus.Registerer) *devbuildRepoMetrics {
	if registerer == nil {
		return nil
	}
	denied := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "devbuild_github_repo_denied_total",
		Help: "Number of devbuild requests rejected because githubRepo is not in the allowlist.",
	}, []string{"surface"})
	registerer.MustRegister(denied)
	return &devbuildRepoMetrics{denied: denied}
}

func (m *devbuildRepoMetrics) recordDenied(surface string) {
	if m == nil || m.denied == nil {
		return
	}
	m.denied.WithLabelValues(surface).Inc()
}
