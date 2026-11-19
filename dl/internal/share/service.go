package share

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"io"
	"log"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	api "github.com/PingCAP-QE/ee-apps/dl/gen/shares"
	"github.com/google/uuid"
	"goa.design/goa/v3/security"
)

type Backend interface {
	Resolve(context.Context, string, string, string) (Artifact, error)
	Fetch(context.Context, Artifact) (io.ReadCloser, error)
}

type Service struct {
	Store   Store
	Backend Backend
	Config  *Config
	Logger  *log.Logger
	Metrics Metrics
}

type principalKey struct{}

func (s *Service) APIKeyAuth(ctx context.Context, key string, _ *security.APIKeyScheme) (context.Context, error) {
	p := s.Config.Authenticate(key)
	if p == nil {
		s.Metrics.Denied.Add(1)
		return ctx, api.MakeUnauthorized(errors.New("invalid issuer credentials"))
	}
	enabled, err := s.Store.IssuerEnabled(ctx, p.ID)
	if err != nil {
		return ctx, api.MakeUnavailable(errors.New("delivery unavailable"))
	}
	if !enabled {
		s.Metrics.Denied.Add(1)
		return ctx, api.MakeUnauthorized(errors.New("invalid issuer credentials"))
	}
	return context.WithValue(ctx, principalKey{}, p), nil
}

func validFilename(name string) bool {
	if name == "" || len(name) > 255 || !utf8.ValidString(name) || name == "." || name == ".." || strings.ContainsAny(name, "/\\") {
		return false
	}
	return !strings.ContainsFunc(name, unicode.IsControl)
}

func (s *Service) Create(ctx context.Context, p *api.CreatePayload) (*api.Share, error) {
	issuer, _ := ctx.Value(principalKey{}).(*Policy)
	if issuer == nil {
		return nil, api.MakeUnauthorized(errors.New("invalid issuer credentials"))
	}
	if p.Artifact == nil || !validFilename(p.Artifact.File) || p.Purpose != "customer-delivery" || (p.CustomerID != nil && len(*p.CustomerID) > 255) || (p.MaxSessions != nil && (*p.MaxSessions < 1 || int64(*p.MaxSessions) > 2147483647)) {
		return nil, api.MakeInvalidRequest(errors.New("invalid share request"))
	}
	if !issuer.Allows(p.Artifact.Repository, p.Artifact.Tag) {
		s.Record("", "issue", "denied", 0)
		return nil, api.MakeForbidden(errors.New("artifact is outside issuer policy"))
	}
	ttl := min(int64(86400), issuer.MaxTTLSeconds)
	if p.ExpiresInSeconds != nil {
		ttl = *p.ExpiresInSeconds
	}
	if ttl < 1 || ttl > issuer.MaxTTLSeconds {
		return nil, api.MakeInvalidRequest(errors.New("expiration exceeds issuer policy"))
	}
	a, err := s.Backend.Resolve(ctx, p.Artifact.Repository, p.Artifact.Tag, p.Artifact.File)
	if err != nil {
		return nil, api.MakeInvalidRequest(errors.New("artifact unavailable for delivery"))
	}
	var random [32]byte
	if _, err = rand.Read(random[:]); err != nil {
		return nil, api.MakeUnavailable(errors.New("delivery unavailable"))
	}
	token := base64.RawURLEncoding.EncodeToString(random[:])
	hash := sha256.Sum256([]byte(token))
	g := &Grant{ID: uuid.NewString(), TokenHash: hash[:], Artifact: a, IssuedBy: issuer.ID, Purpose: p.Purpose, MaxSessions: p.MaxSessions}
	if p.CustomerID != nil {
		g.CustomerID = *p.CustomerID
	}
	if err = s.Store.Create(ctx, g, ttl); err != nil {
		return nil, api.MakeUnavailable(errors.New("delivery unavailable"))
	}
	s.Metrics.Issued.Add(1)
	result := result(g)
	downloadURL := s.Config.PublicURL + "/d/" + token
	result.DownloadURL = &downloadURL
	return result, nil
}

func (s *Service) Show(ctx context.Context, p *api.ShowPayload) (*api.Share, error) {
	issuer, _ := ctx.Value(principalKey{}).(*Policy)
	if issuer == nil {
		return nil, api.MakeUnauthorized(errors.New("invalid issuer credentials"))
	}
	g, err := s.Store.Get(ctx, p.ID, issuer.ID)
	if errors.Is(err, ErrNotFound) {
		return nil, api.MakeNotFound(ErrNotFound)
	}
	if err != nil {
		return nil, api.MakeUnavailable(errors.New("delivery unavailable"))
	}
	return result(g), nil
}

func (s *Service) Revoke(ctx context.Context, p *api.RevokePayload) error {
	issuer, _ := ctx.Value(principalKey{}).(*Policy)
	if issuer == nil {
		return api.MakeUnauthorized(errors.New("invalid issuer credentials"))
	}
	err := s.Store.Revoke(ctx, p.ID, issuer.ID)
	if errors.Is(err, ErrNotFound) {
		return api.MakeNotFound(ErrNotFound)
	}
	if err != nil {
		return api.MakeUnavailable(errors.New("delivery unavailable"))
	}
	return nil
}

func result(g *Grant) *api.Share {
	r := &api.Share{ID: g.ID, IssuedBy: g.IssuedBy, CustomerID: &g.CustomerID, Purpose: g.Purpose, CreatedAt: g.CreatedAt.UTC().Format(time.RFC3339), ExpiresAt: g.ExpiresAt.UTC().Format(time.RFC3339), MaxSessions: g.MaxSessions, SessionsStarted: g.SessionsStarted,
		Artifact: &api.PinnedArtifact{Repository: g.Repository, SourceTag: g.SourceTag, ManifestDigest: g.ManifestDigest, BlobDigest: g.BlobDigest, Filename: g.Filename, Size: g.BlobSize, MediaType: g.MediaType}}
	if g.RevokedAt != nil {
		v := g.RevokedAt.UTC().Format(time.RFC3339)
		r.RevokedAt = &v
	}
	return r
}

// Record uses a separate bounded context so client disconnects still produce
// transfer outcomes. Log only fixed event labels and share IDs, never errors.
func (s *Service) Record(id, phase, outcome string, bytes int64) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := s.Store.Event(ctx, id, phase, outcome, bytes); err != nil {
		s.Logger.Print("delivery audit write failed")
	}
	s.Logger.Printf("delivery share_id=%s phase=%s outcome=%s bytes=%d", id, phase, outcome, bytes)
}
