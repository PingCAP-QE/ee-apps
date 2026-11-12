package identity

import (
	"context"
	"net/http"
	"strings"
)

const (
	DefaultUserIDHeader    = "X-User-Id"
	DefaultUserEmailHeader = "X-User-Email"
	DefaultUserNameHeader  = "X-User-Name"
	// Header is retained as an alias for callers that only need the email
	// identity header.
	Header = DefaultUserEmailHeader
)

type contextKey struct{}

// User is the identity associated with a request by the trusted ingress.
type User struct {
	ID    string
	Email string
	Name  string
	// Service marks a principal that represents automation rather than a
	// person. It is set for the configured service identity and never for
	// values read from request headers.
	Service bool
}

// Policy decides whether identity headers may be trusted.
//
// The Gateway is the only authentication layer (see
// docs/gcp-gateway-auth.md in the delivery repo): it validates OIDC sessions
// and ID tokens. It does not yet strip client-supplied X-User-* headers nor
// inject them from verified claims, so until an identity normalizer sits in
// front of the service a header value is a client assertion, not an
// authenticated principal.
//
// TrustHeaders therefore defaults to false. While it is false, headers are
// recorded for audit through AssertedFromContext and never become a principal;
// the service falls back to ServiceIdentity so that automation keeps working
// without anyone being able to impersonate a colleague.
type Policy struct {
	// TrustHeaders accepts the identity headers as a principal. Enable only
	// once the Gateway strips client values and injects verified claims.
	TrustHeaders bool
	// ServiceIdentity is the principal used for requests that carry no
	// trusted user. Empty means "no principal at all".
	ServiceIdentity string
	// ServiceName is the display name of ServiceIdentity.
	ServiceName string
}

// Headers configures the names of identity headers asserted by the trusted
// ingress. Empty values use the standard X-User-* names.
type Headers struct {
	UserID    string
	UserEmail string
	UserName  string
}

func WithUser(ctx context.Context, user User) context.Context {
	return context.WithValue(ctx, contextKey{}, user)
}

func FromContext(ctx context.Context) (User, bool) {
	user, ok := ctx.Value(contextKey{}).(User)
	return user, ok
}

// Middleware copies the identity asserted by the trusted Gateway into the
// request context. Authentication, token validation and claim policy belong to
// the Gateway; this layer only normalizes the identifier for authorization and
// ownership checks in the service.
func Middleware(headers Headers) func(http.Handler) http.Handler {
	if headers.UserID == "" {
		headers.UserID = DefaultUserIDHeader
	}
	if headers.UserEmail == "" {
		headers.UserEmail = DefaultUserEmailHeader
	}
	if headers.UserName == "" {
		headers.UserName = DefaultUserNameHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			user := User{
				ID:    strings.TrimSpace(r.Header.Get(headers.UserID)),
				Email: strings.ToLower(strings.TrimSpace(r.Header.Get(headers.UserEmail))),
				Name:  strings.TrimSpace(r.Header.Get(headers.UserName)),
			}
			if user.ID == "" && user.Email == "" && user.Name == "" {
				next.ServeHTTP(w, r)
				return
			}
			next.ServeHTTP(w, r.WithContext(WithUser(r.Context(), user)))
		})
	}
}

type assertedKey struct{}

// WithAsserted records the raw identity headers of a request without promoting
// them to a principal. It exists so that logs and audit trails can show what a
// caller claimed, even when that claim is not trusted.
func WithAsserted(ctx context.Context, user User) context.Context {
	return context.WithValue(ctx, assertedKey{}, user)
}

// AssertedFromContext returns the identity headers as sent by the caller. Never
// use it for an authorization decision: it is attacker-controlled unless
// Policy.TrustHeaders is set, and even then only the Gateway may have written
// it.
func AssertedFromContext(ctx context.Context) (User, bool) {
	user, ok := ctx.Value(assertedKey{}).(User)
	return user, ok
}

// NewMiddleware applies Policy when resolving the principal of a request.
//
// Resolution order:
//  1. the identity headers, but only when Policy.TrustHeaders is set;
//  2. otherwise Policy.ServiceIdentity, marked User.Service.
//
// A request with neither ends up with no principal, which callers see as
// FromContext returning false.
func NewMiddleware(headers Headers, policy Policy) func(http.Handler) http.Handler {
	if headers.UserID == "" {
		headers.UserID = DefaultUserIDHeader
	}
	if headers.UserEmail == "" {
		headers.UserEmail = DefaultUserEmailHeader
	}
	if headers.UserName == "" {
		headers.UserName = DefaultUserNameHeader
	}
	service := User{
		Email:   strings.ToLower(strings.TrimSpace(policy.ServiceIdentity)),
		Name:    strings.TrimSpace(policy.ServiceName),
		Service: true,
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			asserted := User{
				ID:    strings.TrimSpace(r.Header.Get(headers.UserID)),
				Email: strings.ToLower(strings.TrimSpace(r.Header.Get(headers.UserEmail))),
				Name:  strings.TrimSpace(r.Header.Get(headers.UserName)),
			}
			ctx := r.Context()
			if asserted.ID != "" || asserted.Email != "" || asserted.Name != "" {
				ctx = WithAsserted(ctx, asserted)
			}
			switch {
			case policy.TrustHeaders && (asserted.Email != "" || asserted.ID != ""):
				ctx = WithUser(ctx, asserted)
			case service.Email != "":
				ctx = WithUser(ctx, service)
			}
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}
