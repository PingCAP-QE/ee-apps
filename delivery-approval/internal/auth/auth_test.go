package auth

import (
	"context"
	"errors"
	"testing"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"google.golang.org/api/idtoken"
)

func TestEmailAllowed(t *testing.T) {
	domains := []string{"example.com", "corp.example"}
	for _, email := range []string{"a@example.com", "A@Example.COM", "a@corp.example"} {
		if !EmailAllowed(email, domains) {
			t.Fatalf("%q should be allowed", email)
		}
	}
	for _, email := range []string{"a@sub.example.com", "a@example.com.evil", "a@", "@example.com", "a", ""} {
		if EmailAllowed(email, domains) {
			t.Fatalf("%q should be denied", email)
		}
	}
}

func TestClaimBool(t *testing.T) {
	for _, value := range []any{true, "true", "TRUE"} {
		if !claimBool(value) {
			t.Fatalf("%v should be true", value)
		}
	}
	for _, value := range []any{false, "false", "1", nil, 1} {
		if claimBool(value) {
			t.Fatalf("%v should be false", value)
		}
	}
}

type fakeValidator struct {
	identity *Identity
	err      error
}

func (f fakeValidator) Validate(context.Context, string) (*Identity, error) {
	return f.identity, f.err
}

func TestIdentityContext(t *testing.T) {
	ctx := context.Background()
	if _, ok := IdentityFrom(ctx); ok {
		t.Fatal("empty context has an identity")
	}
	want := &Identity{Email: "a@example.com", Subject: "sub"}
	got, ok := IdentityFrom(WithIdentity(ctx, want))
	if !ok || got.Email != want.Email || got.Subject != want.Subject {
		t.Fatalf("identity = %+v, %v", got, ok)
	}
}

func TestGoogleValidatorFailsClosed(t *testing.T) {
	v := NewGoogleValidator(nil, "")
	if _, err := v.Validate(context.Background(), ""); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("empty token error = %v", err)
	}
	if _, err := v.Validate(context.Background(), "token"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("nil config error = %v", err)
	}
}

func TestFakeValidator(t *testing.T) {
	v := fakeValidator{identity: &Identity{Email: "a@example.com"}}
	got, err := v.Validate(context.Background(), "any")
	if err != nil || got.Email != "a@example.com" {
		t.Fatalf("identity = %+v, err = %v", got, err)
	}
}

func testManager(t *testing.T) *config.Manager {
	t.Helper()
	cfg := config.Default()
	cfg.PublicURL = "https://approvals.example.com"
	cfg.DLBaseURL = "http://dl-management:80"
	cfg.OIDCClientID = "audience"
	cfg.AllowedEmailDomains = []string{"example.com"}
	cfg.ApproverEmails = []string{"approver@example.com"}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	return config.NewManager(cfg)
}

func TestGoogleValidatorClaims(t *testing.T) {
	mgr := testManager(t)
	tests := []struct {
		name    string
		claims  map[string]any
		wantErr error
		want    string
	}{
		{name: "valid", claims: map[string]any{"email": "User@Example.com", "email_verified": true}, want: "user@example.com"},
		{name: "string verified", claims: map[string]any{"email": "user@example.com", "email_verified": "true"}, want: "user@example.com"},
		{name: "unverified", claims: map[string]any{"email": "user@example.com", "email_verified": false}, wantErr: ErrUnauthenticated},
		{name: "missing email", claims: map[string]any{"email_verified": true}, wantErr: ErrUnauthenticated},
		{name: "wrong domain", claims: map[string]any{"email": "user@evil.com", "email_verified": true}, wantErr: ErrForbidden},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v := &GoogleValidator{
				config:   mgr,
				audience: "audience",
				validate: func(context.Context, string, string) (*idtoken.Payload, error) {
					return &idtoken.Payload{Subject: "sub", Claims: tt.claims}, nil
				},
			}
			got, err := v.Validate(context.Background(), "token")
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("error = %v, want %v", err, tt.wantErr)
			}
			if tt.want != "" && (got == nil || got.Email != tt.want || got.Subject != "sub") {
				t.Fatalf("identity = %+v", got)
			}
		})
	}
}

func TestGoogleValidatorRejectsTokenError(t *testing.T) {
	mgr := testManager(t)
	v := &GoogleValidator{
		config:   mgr,
		audience: "audience",
		validate: func(context.Context, string, string) (*idtoken.Payload, error) {
			return nil, errors.New("bad signature")
		},
	}
	if _, err := v.Validate(context.Background(), "token"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("error = %v", err)
	}
}
