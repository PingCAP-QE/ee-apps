package share

import (
	"context"
	"errors"
	"time"

	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

var ErrNotFound = errors.New("share unavailable")

type Artifact struct {
	Repository     string
	SourceTag      string
	ManifestDigest string
	BlobDigest     string
	BlobSize       int64
	MediaType      string
	Filename       string
}

func (a Artifact) Descriptor() ocispec.Descriptor {
	return ocispec.Descriptor{Digest: digest.Digest(a.BlobDigest), Size: a.BlobSize, MediaType: a.MediaType}
}

type Grant struct {
	ID        string
	TokenHash []byte
	Artifact
	IssuedBy        string
	CustomerID      string
	Purpose         string
	CreatedAt       time.Time
	ExpiresAt       time.Time
	RevokedAt       *time.Time
	MaxSessions     *int
	SessionsStarted int
}

// Store authorizes each request against shared state. A GET reserves a session
// atomically; HEAD validates the same quota but never increments it.
type Store interface {
	IssuerEnabled(context.Context, string) (bool, error)
	Create(context.Context, *Grant, int64) error
	Get(context.Context, string, string) (*Grant, error)
	Revoke(context.Context, string, string) error
	Authorize(context.Context, []byte, bool) (*Grant, error)
	Event(context.Context, string, string, string, int64) error
}
