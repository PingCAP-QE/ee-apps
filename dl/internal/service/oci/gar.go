package oci

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	"oras.land/oras-go/v2/registry/remote/auth"
)

// artifactRegistryScope is the OAuth scope needed to pull from Artifact
// Registry with an access token.
const artifactRegistryScope = "https://www.googleapis.com/auth/cloud-platform"

// artifactRegistrySuite is the registry hostname suffix served by Artifact
// Registry (us-docker.pkg.dev, asia-....pkg.dev, ...).
const artifactRegistrySuite = ".pkg.dev"

// tokenMinter returns a registry credential. It is an interface so tests can
// avoid the metadata server.
type tokenMinter interface {
	credential(context.Context) (auth.Credential, error)
}

// garTokenSource mints and caches an Artifact Registry access token from the
// Pod's Google identity (Workload Identity or ADC). Tokens are refreshed
// shortly before they expire; concurrent callers share one refresh.
type garTokenSource struct {
	mu  sync.Mutex
	src oauth2.TokenSource
	tok *oauth2.Token
}

func (t *garTokenSource) credential(ctx context.Context) (auth.Credential, error) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.src == nil {
		src, err := google.DefaultTokenSource(ctx, artifactRegistryScope)
		if err != nil {
			return auth.EmptyCredential, fmt.Errorf("artifact registry credentials unavailable: %w", err)
		}
		t.src = src
	}
	if t.tok == nil || !t.tok.Valid() || time.Until(t.tok.Expiry) < 5*time.Minute {
		tok, err := t.src.Token()
		if err != nil {
			return auth.EmptyCredential, fmt.Errorf("artifact registry token: %w", err)
		}
		t.tok = tok
	}
	// Artifact Registry accepts the Docker token flow with this username and an
	// OAuth2 access token as the password.
	return auth.Credential{Username: "oauth2accesstoken", Password: t.tok.AccessToken}, nil
}

// credentialFor resolves the credential for one repository host. A configured
// username/password always wins; otherwise Artifact Registry hosts get a minted
// token when enabled; everything else stays anonymous so public repositories
// keep working without any stored secret.
func (s *ocisrvc) credentialFor(ctx context.Context, registry string) (auth.Credential, error) {
	s.mu.RLock()
	static := *s.credential
	adcEnabled := s.artifactRegistryADC
	s.mu.RUnlock()

	if static.Username != "" || static.Password != "" {
		return static, nil
	}
	if !adcEnabled || !isArtifactRegistry(registry) {
		return auth.EmptyCredential, nil
	}
	tokens := s.tokens
	if tokens == nil {
		tokens = &garTokenSource{}
	}
	cred, err := tokens.credential(ctx)
	if err != nil {
		// Degrade to anonymous instead of failing the request: public
		// repositories still work, and a private repository answers 401, which
		// the registry itself enforces. A misconfigured identity must not take
		// every target down.
		s.logger.Printf("oci: artifact registry credential unavailable, falling back to anonymous: %v", err)
		return auth.EmptyCredential, nil
	}
	return cred, nil
}

func isArtifactRegistry(registry string) bool {
	return strings.HasSuffix(strings.ToLower(strings.TrimSuffix(registry, ".")), artifactRegistrySuite)
}

var _ tokenMinter = (*garTokenSource)(nil)
