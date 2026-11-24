package identity

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/lestrrat-go/jwx/v4/jwa"
	"github.com/lestrrat-go/jwx/v4/jwk"
	"github.com/lestrrat-go/jwx/v4/jwt"
)

func TestJWTVerifierValidatesSignatureIssuerAudienceAndExpiry(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	publicJWK, err := jwk.Import[jwk.Key](&privateKey.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := publicJWK.Set(jwk.KeyIDKey, "key-1"); err != nil {
		t.Fatal(err)
	}
	if err := publicJWK.Set(jwk.AlgorithmKey, jwa.RS256()); err != nil {
		t.Fatal(err)
	}
	set := jwk.NewSet()
	set.AddKey(publicJWK)
	keySetJSON, err := json.Marshal(set)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(keySetJSON)
	}))
	defer server.Close()

	verifier, err := NewJWTVerifier(ctx, JWTConfig{
		JWKSURL: server.URL, Issuer: "https://issuer.example", Audience: "authzd-test",
		PrincipalPrefix: "user:https://issuer.example|", SubjectClaim: "sub",
	})
	if err != nil {
		t.Fatalf("NewJWTVerifier() error = %v", err)
	}
	t.Cleanup(func() { _ = verifier.Close(context.Background()) })

	for _, tc := range []struct {
		name        string
		issuer      string
		audience    []string
		expires     time.Time
		key         any
		wantErr     bool
		wantSubject string
	}{
		{name: "valid", issuer: "https://issuer.example", audience: []string{"authzd-test"}, expires: time.Now().Add(time.Minute), key: privateKey, wantSubject: "user:https://issuer.example|user-123"},
		{name: "wrong issuer", issuer: "https://attacker.example", audience: []string{"authzd-test"}, expires: time.Now().Add(time.Minute), key: privateKey, wantErr: true},
		{name: "wrong audience", issuer: "https://issuer.example", audience: []string{"other"}, expires: time.Now().Add(time.Minute), key: privateKey, wantErr: true},
		{name: "expired", issuer: "https://issuer.example", audience: []string{"authzd-test"}, expires: time.Now().Add(-time.Minute), key: privateKey, wantErr: true},
		{name: "bad signature", issuer: "https://issuer.example", audience: []string{"authzd-test"}, expires: time.Now().Add(time.Minute), key: mustRSAKey(t), wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			token := signedToken(t, tc.key, tc.issuer, tc.audience, tc.expires)
			principal, err := verifier.Verify(ctx, token)
			if tc.wantErr {
				if err == nil {
					t.Fatal("Verify() succeeded, want validation error")
				}
				return
			}
			if err != nil {
				t.Fatalf("Verify() error = %v", err)
			}
			if principal.Subject != tc.wantSubject {
				t.Fatalf("principal = %q, want %q", principal.Subject, tc.wantSubject)
			}
		})
	}
}

func signedToken(t *testing.T, key any, issuer string, audience []string, expires time.Time) string {
	t.Helper()
	claims, err := jwt.NewBuilder().Issuer(issuer).Subject("user-123").Audience(audience).Expiration(expires).Build()
	if err != nil {
		t.Fatal(err)
	}
	var signingKey any = key
	if privateKey, ok := key.(*rsa.PrivateKey); ok {
		jwkKey, err := jwk.Import[jwk.Key](privateKey)
		if err != nil {
			t.Fatal(err)
		}
		_ = jwkKey.Set(jwk.KeyIDKey, "key-1")
		_ = jwkKey.Set(jwk.AlgorithmKey, jwa.RS256())
		signingKey = jwkKey
	}
	signed, err := jwt.Sign(claims, jwt.WithKey(jwa.RS256(), signingKey))
	if err != nil {
		t.Fatal(err)
	}
	return string(signed)
}

func mustRSAKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func TestNewJWTVerifierRejectsIncompleteOrInsecureConfiguration(t *testing.T) {
	base := JWTConfig{
		JWKSURL: "https://issuer.example/jwks", Issuer: "https://issuer.example",
		Audience: "authzd", PrincipalPrefix: "user:https://issuer.example|",
	}
	for _, tc := range []struct {
		name   string
		config JWTConfig
	}{
		{name: "missing issuer", config: JWTConfig{JWKSURL: base.JWKSURL, Audience: base.Audience, PrincipalPrefix: base.PrincipalPrefix}},
		{name: "non-https issuer", config: JWTConfig{JWKSURL: base.JWKSURL, Issuer: "http://issuer.example", Audience: base.Audience, PrincipalPrefix: base.PrincipalPrefix}},
		{name: "non-https remote JWKS", config: JWTConfig{JWKSURL: "http://issuer.example/jwks", Issuer: base.Issuer, Audience: base.Audience, PrincipalPrefix: base.PrincipalPrefix}},
		{name: "JWKS URL contains query credentials", config: JWTConfig{JWKSURL: "https://issuer.example/jwks?token=secret", Issuer: base.Issuer, Audience: base.Audience, PrincipalPrefix: base.PrincipalPrefix}},
		{name: "unsupported principal type", config: JWTConfig{JWKSURL: base.JWKSURL, Issuer: base.Issuer, Audience: base.Audience, PrincipalPrefix: "admin:"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewJWTVerifier(context.Background(), tc.config); err == nil {
				t.Fatal("NewJWTVerifier() accepted unsafe configuration")
			}
		})
	}
}

func TestIsLoopbackHost(t *testing.T) {
	for host, want := range map[string]bool{
		"localhost": true, "127.0.0.1": true, "::1": true,
		"127.0.0.1.attacker.invalid": false, "issuer.example": false,
	} {
		if got := isLoopbackHost(host); got != want {
			t.Errorf("isLoopbackHost(%q) = %t, want %t", host, got, want)
		}
	}
}
