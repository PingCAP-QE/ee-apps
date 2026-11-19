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
	if s.policyMetrics != nil {
		s.policyMetrics.recordRepoDenied(surface)
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

// recordBuilderImageDenied is the builder image counterpart of
// recordRepoDenied: the same counter shape and the same fields, so one query
// shows every policy rejection. The allowlist content is deliberately not
// logged.
func (s *devbuildsrvc) recordBuilderImageDenied(surface, image, product, createdBy string) {
	if s.policyMetrics != nil {
		s.policyMetrics.recordBuilderImageDenied(surface)
	}
	if s.logger != nil {
		s.logger.Warn().
			Str("surface", surface).
			Str("builder_img", image).
			Str("product", product).
			Str("created_by", createdBy).
			Msg("devbuild builderImg rejected by the allowlist")
	}
}

// isAllowlistDenied reports whether err is a policy rejection, so the callers
// can record it once at the point where they know the request context.
func isAllowlistDenied(err error) bool {
	var forbidden *devbuild.DevBuildForbiddenError
	return errors.As(err, &forbidden)
}

// devbuildPolicyMetrics holds the devbuild policy counters. A nil registry keeps
// every method a no-op, which is what tests and the metrics-disabled API need.
type devbuildPolicyMetrics struct {
	repoDenied         *prometheus.CounterVec
	builderImageDenied *prometheus.CounterVec
}

func newDevbuildPolicyMetrics(registerer prometheus.Registerer) *devbuildPolicyMetrics {
	if registerer == nil {
		return nil
	}
	repoDenied := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "devbuild_github_repo_denied_total",
		Help: "Number of devbuild requests rejected because githubRepo is not in the allowlist.",
	}, []string{"surface"})
	builderImageDenied := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "devbuild_builder_image_denied_total",
		Help: "Number of devbuild requests rejected because builderImg is not in the allowlist.",
	}, []string{"surface"})
	registerer.MustRegister(repoDenied, builderImageDenied)
	return &devbuildPolicyMetrics{repoDenied: repoDenied, builderImageDenied: builderImageDenied}
}

func (m *devbuildPolicyMetrics) recordRepoDenied(surface string) {
	if m == nil || m.repoDenied == nil {
		return
	}
	m.repoDenied.WithLabelValues(surface).Inc()
}

func (m *devbuildPolicyMetrics) recordBuilderImageDenied(surface string) {
	if m == nil || m.builderImageDenied == nil {
		return
	}
	m.builderImageDenied.WithLabelValues(surface).Inc()
}
