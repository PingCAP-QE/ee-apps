package server

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authn"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/ratelimit"
)

// Middleware authenticates callers before they reach the generated service
// endpoints and applies the per-caller rate limit.
type Middleware struct {
	reviewer  authn.Reviewer
	rateLimit *ratelimit.Limiter
	config    config.Provider
	metrics   *metrics.Metrics
	logger    *slog.Logger
}

// NewMiddleware creates the authentication middleware.
func NewMiddleware(reviewer authn.Reviewer, rateLimit *ratelimit.Limiter, configProvider config.Provider, brokerMetrics *metrics.Metrics, logger *slog.Logger) *Middleware {
	if logger == nil {
		logger = slog.Default()
	}
	return &Middleware{
		reviewer:  reviewer,
		rateLimit: rateLimit,
		config:    configProvider,
		metrics:   brokerMetrics,
		logger:    logger,
	}
}

// Wrap protects the token endpoint. Health and metrics endpoints are passed
// through untouched.
func (m *Middleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/token" {
			next.ServeHTTP(w, r)
			return
		}

		caller, err := m.authenticate(r)
		if err != nil {
			m.metrics.Request("", false)
			m.metrics.Error("unauthenticated")
			status := http.StatusUnauthorized
			if !errors.Is(err, authn.ErrUnauthenticated) {
				status = http.StatusInternalServerError
			}
			writeError(w, status, "unauthorized", "caller is not authenticated")
			return
		}

		if m.rateLimit != nil && m.config != nil {
			if cfg := m.config.Current(); cfg != nil {
				m.rateLimit.SetPerMinute(cfg.RateLimit.PerMinute)
			}
		}
		key := caller.Namespace + "/" + caller.ServiceAccount
		if m.rateLimit != nil && !m.rateLimit.Allow(key) {
			m.metrics.RateLimited()
			m.metrics.Request("", false)
			m.metrics.Error("rate_limited")
			w.Header().Set("Retry-After", "60")
			writeError(w, http.StatusTooManyRequests, "rate_limited", "rate limit exceeded")
			return
		}

		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(recorder, r.WithContext(authn.WithCaller(r.Context(), caller)))
		if recorder.status >= http.StatusBadRequest {
			m.metrics.Request("", false)
			m.metrics.Error(errorReason(recorder.status))
		}
	})
}

func (m *Middleware) authenticate(r *http.Request) (authz.Caller, error) {
	header := r.Header.Get("Authorization")
	if header == "" {
		return authz.Caller{}, authn.ErrUnauthenticated
	}
	token, ok := strings.CutPrefix(header, "Bearer ")
	if !ok || strings.TrimSpace(token) == "" {
		return authz.Caller{}, authn.ErrUnauthenticated
	}
	caller, err := m.reviewer.Review(r.Context(), strings.TrimSpace(token))
	if err != nil {
		if errors.Is(err, authn.ErrUnauthenticated) {
			return authz.Caller{}, authn.ErrUnauthenticated
		}
		m.logger.Error("authentication backend failed", "error", err)
		return authz.Caller{}, err
	}
	return caller, nil
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func writeError(w http.ResponseWriter, status int, name, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"name": name, "message": message})
}

func errorReason(status int) string {
	switch status {
	case http.StatusBadRequest:
		return "bad_request"
	case http.StatusUnauthorized:
		return "unauthenticated"
	case http.StatusForbidden:
		return "forbidden"
	case http.StatusNotFound:
		return "not_installed"
	case http.StatusTooManyRequests:
		return "rate_limited"
	case http.StatusGatewayTimeout:
		return "timeout"
	case http.StatusBadGateway:
		return "upstream"
	default:
		return "internal"
	}
}
