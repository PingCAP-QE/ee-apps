package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
)

// targetState is the outcome of looking up the destination of an image copy.
type targetState int

const (
	// targetAbsent means the tag does not exist yet: copy it.
	targetAbsent targetState = iota
	// targetSameDigest means the tag already points at exactly this image: the
	// copy is a no-op, so a retry or a re-run stays idempotent.
	targetSameDigest
)

// guardTarget decides whether an image copy may proceed.
//
// The TiUP path refuses to overwrite an existing formal version; the image path
// had no equivalent, so a sync could republish an older, different build over a
// tag consumers already pull. Comparing digests rather than mere existence is
// what keeps retries and deliberate re-syncs of the same image working.
//
// A failure to look the destination up is returned as an error: refusing to copy
// is the safe direction when we cannot tell whether the tag is already taken.
func guardTarget(ctx context.Context, source, target string) (targetState, error) {
	srcRef, err := name.ParseReference(source)
	if err != nil {
		return targetAbsent, fmt.Errorf("parse source image %q: %w", source, err)
	}
	dstRef, err := name.ParseReference(target)
	if err != nil {
		return targetAbsent, fmt.Errorf("parse target image %q: %w", target, err)
	}

	opts := []remote.Option{
		remote.WithContext(ctx),
		remote.WithAuthFromKeychain(authn.DefaultKeychain),
	}

	src, err := remote.Head(srcRef, opts...)
	if err != nil {
		return targetAbsent, fmt.Errorf("resolve source image %s: %w", source, err)
	}

	dst, err := remote.Head(dstRef, opts...)
	if err != nil {
		var terr *transport.Error
		if errors.As(err, &terr) && terr.StatusCode == http.StatusNotFound {
			return targetAbsent, nil
		}
		return targetAbsent, fmt.Errorf("resolve target image %s: %w", target, err)
	}

	if dst.Digest == src.Digest {
		return targetSameDigest, nil
	}
	return targetAbsent, fmt.Errorf(
		"refusing to overwrite %s: it already publishes %s, the source %s is %s",
		target, dst.Digest, source, src.Digest)
}
