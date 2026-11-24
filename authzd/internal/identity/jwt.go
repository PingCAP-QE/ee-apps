package identity

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jwx-go/jwkfetch/v4"
	"github.com/lestrrat-go/httprc/v3"
	"github.com/lestrrat-go/jwx/v4/jwt"
)

type Principal struct {
	Subject string
}

type JWTConfig struct {
	JWKSURL         string
	Issuer          string
	Audience        string
	PrincipalPrefix string
	SubjectClaim    string
}

type JWTVerifier struct {
	config JWTConfig
	cache  *jwkfetch.Cache
}

func NewJWTVerifier(ctx context.Context, cfg JWTConfig) (*JWTVerifier, error) {
	if cfg.Issuer == "" || cfg.Audience == "" || cfg.PrincipalPrefix == "" {
		return nil, errors.New("issuer, audience, and principal prefix are required")
	}
	issuer, err := url.Parse(cfg.Issuer)
	if err != nil || issuer.Scheme != "https" || issuer.Host == "" || issuer.User != nil || issuer.RawQuery != "" || issuer.Fragment != "" {
		return nil, errors.New("issuer must be a trusted HTTPS URL without credentials or query parameters")
	}
	if !strings.HasPrefix(cfg.PrincipalPrefix, "user:") && !strings.HasPrefix(cfg.PrincipalPrefix, "workload:") {
		return nil, errors.New("principal prefix must identify a user or workload")
	}
	if cfg.SubjectClaim == "" {
		cfg.SubjectClaim = "sub"
	}
	u, err := url.Parse(cfg.JWKSURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && !(u.Scheme == "http" && isLoopbackHost(u.Hostname()))) {
		return nil, errors.New("JWKS URL must use HTTPS (HTTP is allowed only for loopback tests)")
	}
	client := &http.Client{
		Timeout: 10 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 || req.URL.Scheme != u.Scheme || !strings.EqualFold(req.URL.Host, u.Host) {
				return http.ErrUseLastResponse
			}
			return nil
		},
	}
	cache, err := jwkfetch.NewCache(ctx, httprc.NewClient(),
		jwkfetch.WithHTTPClient(jwkfetch.WrapHTTPClientDefaults(client)),
		jwkfetch.WithMaxBodySize(1<<20),
	)
	if err != nil {
		return nil, fmt.Errorf("create JWKS cache: %w", err)
	}
	if err := cache.Register(ctx, cfg.JWKSURL, jwkfetch.WithWaitReady(true)); err != nil {
		_ = cache.Shutdown(context.Background())
		return nil, errors.New("register JWKS endpoint")
	}
	return &JWTVerifier{config: cfg, cache: cache}, nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (v *JWTVerifier) Verify(ctx context.Context, raw string) (Principal, error) {
	if strings.TrimSpace(raw) == "" {
		return Principal{}, errors.New("missing identity token")
	}
	keys, err := v.cache.Lookup(ctx, v.config.JWKSURL)
	if err != nil {
		return Principal{}, errors.New("identity key set unavailable")
	}
	token, err := jwt.ParseString(raw,
		jwt.WithKeySet(keys),
		jwt.WithIssuer(v.config.Issuer),
		jwt.WithAudience(v.config.Audience),
		jwt.WithRequiredClaim("exp"),
		jwt.WithRequiredClaim("sub"),
	)
	if err != nil {
		return Principal{}, errors.New("identity token validation failed")
	}
	value, ok := token.Field(v.config.SubjectClaim)
	if !ok {
		return Principal{}, errors.New("identity token is missing its configured subject claim")
	}
	subject, ok := value.(string)
	if !ok || strings.TrimSpace(subject) == "" || strings.ContainsAny(subject, "\r\n") {
		return Principal{}, errors.New("identity token has an invalid subject claim")
	}
	return Principal{Subject: v.config.PrincipalPrefix + subject}, nil
}

func (v *JWTVerifier) Close(ctx context.Context) error {
	return v.cache.Shutdown(ctx)
}
