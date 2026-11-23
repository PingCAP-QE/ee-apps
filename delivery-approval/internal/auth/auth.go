// Package auth validates the Google ID token that the Gateway forwards in
// X-Approval-Id-Token. It never trusts caller-supplied identity headers.
package auth

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"google.golang.org/api/idtoken"
)

var (
	ErrUnauthenticated = errors.New("unauthenticated")
	ErrForbidden       = errors.New("forbidden")
)

type Identity struct {
	Email   string
	Subject string
}

type Validator interface {
	Validate(ctx context.Context, token string) (*Identity, error)
}

type GoogleValidator struct {
	config   *config.Manager
	audience string
	validate func(context.Context, string, string) (*idtoken.Payload, error)
}

func NewGoogleValidator(config *config.Manager, audience string) *GoogleValidator {
	return &GoogleValidator{config: config, audience: audience, validate: idtoken.Validate}
}

// Validate checks the Google signature/audience via idtoken.Validate and then
// enforces email_verified plus the configured domain allowlist. Any failure is
// fail-closed.
func (v *GoogleValidator) Validate(ctx context.Context, token string) (*Identity, error) {
	if token == "" || v.audience == "" || v.config == nil || v.validate == nil {
		return nil, ErrUnauthenticated
	}
	validateCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	payload, err := v.validate(validateCtx, token, v.audience)
	if err != nil {
		return nil, ErrUnauthenticated
	}
	email, _ := payload.Claims["email"].(string)
	if email == "" || !claimBool(payload.Claims["email_verified"]) {
		return nil, ErrUnauthenticated
	}
	identity := &Identity{Email: strings.ToLower(email), Subject: payload.Subject}
	if !EmailAllowed(identity.Email, v.config.Current().AllowedEmailDomains) {
		return nil, ErrForbidden
	}
	return identity, nil
}

func claimBool(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		return strings.EqualFold(v, "true")
	default:
		return false
	}
}

// EmailAllowed reports whether the email's domain is in the allowlist. The
// domain comparison is case-insensitive and exact; subdomains are not implied.
func EmailAllowed(email string, domains []string) bool {
	at := strings.LastIndex(email, "@")
	if at <= 0 || at == len(email)-1 {
		return false
	}
	domain := strings.ToLower(email[at+1:])
	for _, allowed := range domains {
		if strings.EqualFold(domain, allowed) {
			return true
		}
	}
	return false
}

type identityKey struct{}

func WithIdentity(ctx context.Context, identity *Identity) context.Context {
	return context.WithValue(ctx, identityKey{}, identity)
}

func IdentityFrom(ctx context.Context) (*Identity, bool) {
	identity, ok := ctx.Value(identityKey{}).(*Identity)
	return identity, ok && identity != nil
}
