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
