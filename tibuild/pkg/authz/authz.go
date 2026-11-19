// Package authz decides whether a request may perform an action.
//
// Authentication belongs to the Gateway (OIDC sessions for browsers, ID tokens
// for machines). What the Gateway cannot see is who is asking inside this
// process: a request that reaches the service directly, from another pod, is
// indistinguishable from one that came through the Gateway unless the Gateway
// asserts the identity. This package therefore keeps the service's own answer
// to "may this principal do this", and fails closed once enforcement is on.
package authz

import (
	"net/http"
	"strings"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
	"github.com/rs/zerolog"
)

// Mode controls whether a decision is enforced or only recorded.
type Mode string

const (
	// ModeOff keeps the pre-existing behaviour: every request is allowed and
	// the decision is recorded for visibility only.
	ModeOff Mode = "off"
	// ModeObserve allows the request and logs what would have been denied. It
	// is the rollout step: run it in a cluster, check the logs, then enforce.
	ModeObserve Mode = "observe"
	// ModeEnforce denies the request.
	ModeEnforce Mode = "enforce"
)

// Action is the class of operation a request performs.
type Action string

const (
	// ActionRead does not change state.
	ActionRead Action = "read"
	// ActionMutate creates, updates or deletes state.
	ActionMutate Action = "mutate"
)

// Policy is the authorization configuration of the service.
type Policy struct {
	// Mode selects off / observe / enforce.
	Mode Mode
	// AllowPrincipals lists the principals allowed to mutate, as exact email
	// addresses or "glob@domain" patterns ("*@pingcap.com"). An empty list
	// allows nobody: the check is fail-closed, so a missing configuration
	// denies rather than opens.
	AllowPrincipals []string
	// Logger records denied and would-be-denied decisions. Nil disables
	// logging.
	Logger *zerolog.Logger
}

// Decision is the outcome of one check.
type Decision struct {
	Allowed bool
	// Reason explains a denial in one short phrase, safe to put in a log.
	Reason string
	// Enforced is false when Mode is off or observe: the caller is allowed
	// through even though the policy would deny.
	Enforced bool
}

// Denied reports a decision that should turn into a response.
func (d Decision) Denied() bool {
	return !d.Allowed && d.Enforced
}

// Decide answers whether principal may perform action.
//
// Reads are always allowed: they are the UI's data path and the Gateway already
// authenticated the caller. Mutations need a principal that the operator
// listed.
func (p Policy) Decide(principal identity.User, hasPrincipal bool, action Action) Decision {
	enforced := p.Mode == ModeEnforce
	if p.Mode == ModeOff {
		// Pre-existing behaviour: nothing is enforced and nothing is recorded.
		return Decision{Allowed: true}
	}
	if action == ActionRead {
		return Decision{Allowed: true, Enforced: enforced}
	}
	if !hasPrincipal || (principal.Email == "" && principal.ID == "") {
		return Decision{Allowed: false, Reason: "no authenticated principal", Enforced: enforced}
	}
	if !p.principalAllowed(principal) {
		return Decision{Allowed: false, Reason: "principal is not allowed to mutate", Enforced: enforced}
	}
	return Decision{Allowed: true, Enforced: enforced}
}

// principalAllowed matches the principal against AllowPrincipals.
//
// A pattern without a leading "*" must match the whole address. A pattern with
// a leading "*" matches its suffix, so "*@pingcap.com" accepts every address in
// that domain. Matching is case-insensitive and never falls back to "allow all"
// on an empty list.
func (p Policy) principalAllowed(principal identity.User) bool {
	email := strings.ToLower(strings.TrimSpace(principal.Email))
	if email == "" {
		// A principal without an email cannot be matched by an email pattern,
		// so it is only allowed when the list names its ID exactly.
		email = strings.ToLower(strings.TrimSpace(principal.ID))
	}
	if email == "" {
		return false
	}
	for _, pattern := range p.AllowPrincipals {
		pattern = strings.ToLower(strings.TrimSpace(pattern))
		if pattern == "" {
			continue
		}
		if suffix, ok := strings.CutPrefix(pattern, "*"); ok {
			if strings.HasSuffix(email, suffix) {
				return true
			}
			continue
		}
		if email == pattern {
			return true
		}
	}
	return false
}

// logger returns a usable logger even when none was configured.
func (p Policy) logger() zerolog.Logger {
	if p.Logger == nil {
		return zerolog.Nop()
	}
	return *p.Logger
}

// ActionForMethod classifies an HTTP method.
func ActionForMethod(method string) Action {
	switch method {
	case http.MethodGet, http.MethodHead, http.MethodOptions:
		return ActionRead
	default:
		return ActionMutate
	}
}

// Middleware applies the policy to every request.
//
// It is meant to sit directly behind identity.NewMiddleware, which is what
// puts the principal in the context. A denied mutation answers 401 when no
// principal was established and 403 when one was but may not mutate.
func (p Policy) Middleware(next http.Handler) http.Handler {
	if p.Mode == "" {
		p.Mode = ModeOff
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, has := identity.FromContext(r.Context())
		action := ActionForMethod(r.Method)
		decision := p.Decide(principal, has, action)
		if !decision.Allowed {
			logger := p.logger().With().
				Str("method", r.Method).
				Str("path", r.URL.Path).
				Str("action", string(action)).
				Str("reason", decision.Reason).
				Str("mode", string(p.Mode)).
				Str("principal", principal.Email).
				Bool("service", principal.Service).
				Bool("enforced", decision.Enforced).
				Logger()
			if decision.Enforced {
				logger.Warn().Msg("authorization denied")
				status := http.StatusForbidden
				if !has || (principal.Email == "" && principal.ID == "") {
					status = http.StatusUnauthorized
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(status)
				_, _ = w.Write([]byte(`{"name":"unauthorized","message":"` + decision.Reason + `"}`))
				return
			}
			logger.Info().Msg("authorization would deny (observe mode)")
		}
		next.ServeHTTP(w, r)
	})
}
