package impl

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// Publication kinds and their step payloads.
const (
	// PublishKindImageCopy copies one source digest into N destination
	// repositories. Targets are independent and run in parallel.
	PublishKindImageCopy = "image_copy"
)

// ErrNothingToPublish means the rules file has no destination for the
// reference: the request is a successful no-op, exactly like the legacy
// generator writing no script.
var ErrNothingToPublish = errors.New("no delivery rule matches the image reference")

// ImageCopyPayload is the payload of a kind=image_copy step. It is complete:
// any worker with the credentials of Target can replay it without consulting
// the planner again.
type ImageCopyPayload struct {
	// Source is the repository pinned to the planned digest, e.g.
	// us-docker.pkg.dev/project/hub/pingcap/ticdc/image@sha256:...
	Source string `json:"source"`
	// Digest is the pinned digest, kept separate for readability.
	Digest string `json:"digest"`
	// Dest is the destination repository.
	Dest string `json:"dest"`
	// Tags are the tags to write on Dest; the first one is the source tag.
	Tags []string `json:"tags"`
}

// imageOps is the registry surface the planner and the executor need. It is an
// interface so tests can run without a registry.
type imageOps interface {
	// Digest resolves a reference to its content digest.
	Digest(ctx context.Context, ref string) (string, error)
	// Copy copies src (pinned to a digest) into dst.
	Copy(ctx context.Context, src, dst string) error
}

// craneImageOps talks to the registries through go-containerregistry, using the
// credentials of the process (docker config.json mounted into the pod).
type craneImageOps struct{}

func (craneImageOps) Digest(ctx context.Context, ref string) (string, error) {
	digest, err := crane.Digest(ref, crane.WithContext(ctx))
	if err != nil {
		return "", err
	}
	if digest == "" {
		return "", fmt.Errorf("registry returned an empty digest")
	}
	return digest, nil
}

func (craneImageOps) Copy(ctx context.Context, src, dst string) error {
	return crane.Copy(src, dst, crane.WithContext(ctx))
}

// resolveError marks failures of the source registry lookup, so the API can
// answer 502 instead of 500.
type resolveError struct{ err error }

func (e resolveError) Error() string { return e.err.Error() }
func (e resolveError) Unwrap() error { return e.err }

// rulesLoader reads the delivery rules and reloads them when the file changes.
// The rules are synced into the cluster, so a restart is not required.
type rulesLoader struct {
	path string

	mu      sync.Mutex
	cached  *delivery.Config
	modTime time.Time
	size    int64
}

// setPath points the loader at a new rules file. The next read re-parses it.
func (l *rulesLoader) setPath(path string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.path == path {
		return
	}
	l.path, l.cached, l.modTime, l.size = path, nil, time.Time{}, 0
}

func (l *rulesLoader) config() (*delivery.Config, error) {
	l.mu.Lock()
	defer l.mu.Unlock()

	info, err := os.Stat(l.path)
	if err != nil {
		return nil, fmt.Errorf("delivery rules: %w", err)
	}
	if l.cached != nil && info.ModTime().Equal(l.modTime) && info.Size() == l.size {
		return l.cached, nil
	}

	cfg, err := delivery.LoadFile(l.path)
	if err != nil {
		return nil, err
	}
	l.cached, l.modTime, l.size = cfg, info.ModTime(), info.Size()
	return cfg, nil
}

// plannedStep is one destination of a publication plan.
type plannedStep struct {
	Target  string
	Dest    string
	Payload ImageCopyPayload
}

// publicationPlan is the outcome of planning one image reference.
type publicationPlan struct {
	ArtifactURL string
	Digest      string
	Steps       []plannedStep
}

// publishPlanner turns an image reference into the work to execute.
type publishPlanner struct {
	rules    *rulesLoader
	resolver imageOps
}

// setRulesPath points the planner at a new delivery rules file.
func (p *publishPlanner) setRulesPath(path string) {
	if p.rules == nil {
		p.rules = &rulesLoader{}
	}
	p.rules.setPath(path)
}

// plan expands an image reference.
//
// The digest is resolved exactly once here and written into every step, so the
// targets cannot disagree about the content when the source tag moves while the
// unit is in flight.
func (p *publishPlanner) plan(ctx context.Context, src string) (*publicationPlan, error) {
	ref, err := delivery.ParseImageRef(src)
	if err != nil {
		return nil, err
	}

	cfg, err := p.rules.config()
	if err != nil {
		return nil, err
	}

	plans, err := delivery.MatchImage(cfg, src)
	if err != nil {
		return nil, err
	}
	if len(plans) == 0 {
		return nil, ErrNothingToPublish
	}

	digest := ref.Digest
	if digest == "" {
		digest, err = p.resolver.Digest(ctx, ref.Repository+":"+ref.Tag)
		if err != nil {
			return nil, resolveError{err: fmt.Errorf("resolve digest of %s: %w", src, err)}
		}
	}

	// Several rules can reach the same destination repository (the legacy
	// generator emitted a duplicate copy for those). One step per destination
	// is enough: the target receives the same digest either way.
	steps := make([]plannedStep, 0, len(plans))
	index := make(map[string]int, len(plans))
	for _, plan := range plans {
		if i, ok := index[plan.Dest]; ok {
			steps[i].Payload.Tags = appendUnique(steps[i].Payload.Tags, plan.Tags...)
			continue
		}
		index[plan.Dest] = len(steps)
		steps = append(steps, plannedStep{
			Target: plan.Target,
			Dest:   plan.Dest,
			Payload: ImageCopyPayload{
				Source: ref.Repository + "@" + digest,
				Digest: digest,
				Dest:   plan.Dest,
				Tags:   appendUnique(nil, plan.Tags...),
			},
		})
	}

	return &publicationPlan{
		ArtifactURL: src,
		Digest:      digest,
		Steps:       steps,
	}, nil
}

func appendUnique(dst []string, values ...string) []string {
	seen := make(map[string]struct{}, len(dst)+len(values))
	for _, value := range dst {
		seen[value] = struct{}{}
	}
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		dst = append(dst, value)
	}
	return dst
}

// payloadMap converts a typed payload into the JSON object Ent stores.
func payloadMap(payload any) (map[string]any, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// decodeImageCopyPayload reads a stored step payload.
func decodeImageCopyPayload(stored map[string]any) (ImageCopyPayload, error) {
	var payload ImageCopyPayload
	raw, err := json.Marshal(stored)
	if err != nil {
		return payload, err
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return payload, err
	}
	return payload, nil
}

// newUUID returns a random RFC 4122 version 4 UUID.
func newUUID() string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		// crypto/rand never fails on supported platforms; fall back to a
		// time-based value rather than panicking in the request path.
		now := time.Now().UnixNano()
		return fmt.Sprintf("%08x-0000-4000-8000-%012x", uint32(now), uint64(now)&0xffffffffffff)
	}
	buf[6] = (buf[6] & 0x0f) | 0x40
	buf[8] = (buf[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", buf[0:4], buf[4:6], buf[6:8], buf[8:10], buf[10:16])
}
