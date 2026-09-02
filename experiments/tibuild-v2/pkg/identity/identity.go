package identity

import (
	"context"
	"net/http"
	"strings"
)

// Header is populated by the trusted ingress after it has authenticated the
// request. The application deliberately treats it as an identity assertion,
// not as a token to validate.
const Header = "X-EE-User-Email"

type contextKey struct{}

// User is the identity associated with a request by the trusted ingress.
type User struct {
	Email string
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
func Middleware() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			email := strings.ToLower(strings.TrimSpace(r.Header.Get(Header)))
			if email == "" {
				next.ServeHTTP(w, r)
				return
			}
			next.ServeHTTP(w, r.WithContext(WithUser(r.Context(), User{Email: email})))
		})
	}
}
