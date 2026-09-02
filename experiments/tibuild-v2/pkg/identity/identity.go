package identity

import (
	"context"
	"crypto/subtle"
	"fmt"
	"net/http"
	"strings"

	"google.golang.org/api/idtoken"
)

const Header = "X-EE-ID-Token"

type contextKey struct{}

// User is the verified company identity associated with a portal request.
type User struct {
	Email string
}

type Options struct {
	Audience     string
	Issuer       string
	HostedDomain string
	EmailDomain  string
}

type Validator interface {
	Validate(context.Context, string, string) (*idtoken.Payload, error)
}

type googleValidator struct{}

func (googleValidator) Validate(ctx context.Context, token, audience string) (*idtoken.Payload, error) {
	return idtoken.Validate(ctx, token, audience)
}

func WithUser(ctx context.Context, user User) context.Context {
	return context.WithValue(ctx, contextKey{}, user)
}

func FromContext(ctx context.Context) (User, bool) {
	user, ok := ctx.Value(contextKey{}).(User)
	return user, ok
}

func Middleware(options Options) func(http.Handler) http.Handler {
	return MiddlewareWithValidator(options, googleValidator{})
}

func MiddlewareWithValidator(options Options, validator Validator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := strings.TrimSpace(r.Header.Get(Header))
			if token == "" {
				next.ServeHTTP(w, r)
				return
			}
			if options.Audience == "" {
				writeUnauthorized(w, "portal identity verification is not configured")
				return
			}
			payload, err := validator.Validate(r.Context(), token, options.Audience)
			if err != nil {
				writeUnauthorized(w, "invalid portal identity")
				return
			}
			if options.Issuer != "" && subtle.ConstantTimeCompare([]byte(payload.Issuer), []byte(options.Issuer)) != 1 {
				writeUnauthorized(w, "invalid identity issuer")
				return
			}
			email, _ := payload.Claims["email"].(string)
			hostedDomain, _ := payload.Claims["hd"].(string)
			verified, _ := payload.Claims["email_verified"].(bool)
			if !verified || email == "" || (options.HostedDomain != "" && hostedDomain != options.HostedDomain) ||
				(options.EmailDomain != "" && !strings.HasSuffix(strings.ToLower(email), "@"+strings.ToLower(options.EmailDomain))) {
				writeUnauthorized(w, "a verified company identity is required")
				return
			}
			next.ServeHTTP(w, r.WithContext(WithUser(r.Context(), User{Email: strings.ToLower(email)})))
		})
	}
}

func writeUnauthorized(w http.ResponseWriter, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = fmt.Fprintf(w, `{"code":401,"message":%q}`, message)
}
