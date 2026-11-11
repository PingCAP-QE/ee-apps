package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
)

// Verification outcomes, also the `result` label of publish_desired_verify_total.
const (
	verifyResultPublished = "published"
	verifyResultMismatch  = "mismatch"
	verifyResultAbsent    = "absent"
	verifyResultError     = "error"
	verifyResultFallback  = "fallback"
)

// nightlyVerifyCacheTTL bounds how often a component manifest is re-read. The
// manifest only changes when the component publishes, so a few minutes of
// staleness cannot hide a lag (the alert threshold is 1.5 windows).
const nightlyVerifyCacheTTL = 5 * time.Minute

// mirrorPublishedFile is what a mirror records for one channel.
type mirrorPublishedFile struct {
	SHA256 string
	URL    string
	Found  bool
}

// mirrorPlatformEntry is one `platforms[<os>/<arch>][<version>]` entry of a
// TiUP component manifest.
type mirrorPlatformEntry struct {
	Hashes struct {
		SHA256 string `json:"sha256"`
	} `json:"hashes"`
	URL    string `json:"url"`
	Yanked bool   `json:"yanked"`
}

// mirrorComponentDoc is the signed component manifest. The nightly channel is a
// single version string; each platform keeps its own entry for it.
type mirrorComponentDoc struct {
	Signed struct {
		ID        string                                    `json:"id"`
		Nightly   string                                    `json:"nightly"`
		Platforms map[string]map[string]mirrorPlatformEntry `json:"platforms"`
	} `json:"signed"`
}

// mirrorSnapshotDoc is the signed snapshot, which names the current version of
// every component manifest (manifests are served as `<version>.<name>.json`).
type mirrorSnapshotDoc struct {
	Signed struct {
		Meta map[string]struct {
			Version int `json:"version"`
		} `json:"meta"`
	} `json:"signed"`
}

// tiupMirrorReader reads the published tarball sha256 from a TiUP mirror's own
// metadata, without downloading the package: one snapshot GET plus one
// component-manifest GET per (mirror, package) per TTL.
type tiupMirrorReader struct {
	client *http.Client
	ttl    time.Duration
	nowFn  func() time.Time

	mu    sync.Mutex
	cache map[string]tiupMirrorCacheEntry
}

type tiupMirrorCacheEntry struct {
	doc       *mirrorComponentDoc
	fetchedAt time.Time
}

func newTiupMirrorReader(ttl time.Duration) *tiupMirrorReader {
	if ttl <= 0 {
		ttl = nightlyVerifyCacheTTL
	}
	return &tiupMirrorReader{
		client: &http.Client{Timeout: 10 * time.Second},
		ttl:    ttl,
		nowFn:  time.Now,
		cache:  map[string]tiupMirrorCacheEntry{},
	}
}

// publishedSHA256 returns the sha256 the mirror records for one channel.
//
// Found is false when the mirror simply has no such entry (unknown component,
// missing version or platform, or a yanked version): that is a fact about the
// mirror, not a transport failure, so it comes back without an error.
func (r *tiupMirrorReader) publishedSHA256(ctx context.Context, mirrorURL, pkg, osName, arch, version string) (mirrorPublishedFile, error) {
	doc, err := r.component(ctx, mirrorURL, pkg)
	if err != nil {
		return mirrorPublishedFile{}, err
	}
	if doc == nil {
		return mirrorPublishedFile{}, nil
	}
	entry, ok := doc.Signed.Platforms[osName+"/"+arch][version]
	if !ok || entry.Yanked || entry.Hashes.SHA256 == "" {
		return mirrorPublishedFile{}, nil
	}
	return mirrorPublishedFile{SHA256: entry.Hashes.SHA256, URL: entry.URL, Found: true}, nil
}

// component returns the component manifest, reusing a cached copy inside the
// TTL. A nil document means the mirror does not know the component.
func (r *tiupMirrorReader) component(ctx context.Context, mirrorURL, pkg string) (*mirrorComponentDoc, error) {
	key := mirrorURL + "|" + pkg
	now := r.nowFn()

	r.mu.Lock()
	cached, ok := r.cache[key]
	r.mu.Unlock()
	if ok && now.Sub(cached.fetchedAt) < r.ttl {
		return cached.doc, nil
	}

	version, err := r.snapshotVersion(ctx, mirrorURL, pkg)
	if err != nil {
		return nil, err
	}
	if version == 0 {
		return nil, nil
	}
	doc, err := r.fetchComponent(ctx, mirrorURL, pkg, version)
	if err != nil {
		return nil, err
	}

	r.mu.Lock()
	r.cache[key] = tiupMirrorCacheEntry{doc: doc, fetchedAt: now}
	r.mu.Unlock()
	return doc, nil
}

func (r *tiupMirrorReader) snapshotVersion(ctx context.Context, mirrorURL, pkg string) (int, error) {
	var snapshot mirrorSnapshotDoc
	if err := r.getJSON(ctx, strings.TrimSuffix(mirrorURL, "/")+"/snapshot.json", &snapshot); err != nil {
		return 0, fmt.Errorf("read snapshot: %w", err)
	}
	return snapshot.Signed.Meta["/"+pkg+".json"].Version, nil
}

func (r *tiupMirrorReader) fetchComponent(ctx context.Context, mirrorURL, pkg string, version int) (*mirrorComponentDoc, error) {
	url := fmt.Sprintf("%s/%d.%s.json", strings.TrimSuffix(mirrorURL, "/"), version, pkg)
	var doc mirrorComponentDoc
	if err := r.getJSON(ctx, url, &doc); err != nil {
		return nil, fmt.Errorf("read component %s: %w", pkg, err)
	}
	return &doc, nil
}

func (r *tiupMirrorReader) getJSON(ctx context.Context, url string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: status %d", url, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// nightlyVerifyResult is one desired row checked against the mirror.
type nightlyVerifyResult struct {
	Row       *ent.PublishDesired
	Published bool
	Result    string
	MirrorSHA string
}

// verifyNightlyRow decides whether the wanted artifact is on the mirror. When
// the row carries no file digest (payloads written before the verification
// existed) or the mirror cannot be read, it falls back to the legacy
// version-string judgement so behaviour only improves.
func (s *publishsrvc) verifyNightlyRow(ctx context.Context, row *ent.PublishDesired) nightlyVerifyResult {
	wanted := desiredFileSHA256(row)
	mirror, url := s.mirrorForTarget(row.Target)
	if wanted == "" || s.tiupMirror == nil || url == "" {
		return nightlyVerifyResult{Row: row, Published: legacyPublished(row), Result: verifyResultFallback}
	}

	started := time.Now()
	got, err := s.tiupMirror.publishedSHA256(ctx, url, row.Pkg, row.Os, row.Arch, row.DesiredVersion)
	if s.metrics != nil {
		s.metrics.observeVerify(mirror, time.Since(started))
	}
	if err != nil {
		s.logger.Warn().Err(err).
			Str("target", row.Target).Str("pkg", row.Pkg).
			Str("os", row.Os).Str("arch", row.Arch).
			Msg("cannot verify the nightly publication against the mirror")
		return nightlyVerifyResult{Row: row, Published: legacyPublished(row), Result: verifyResultError}
	}
	if !got.Found {
		return nightlyVerifyResult{Row: row, Published: false, Result: verifyResultAbsent}
	}
	if got.SHA256 != wanted {
		return nightlyVerifyResult{Row: row, Published: false, Result: verifyResultMismatch, MirrorSHA: got.SHA256}
	}
	return nightlyVerifyResult{Row: row, Published: true, Result: verifyResultPublished, MirrorSHA: got.SHA256}
}

// verifyNightlyRows checks every row, recording one verification sample each.
func (s *publishsrvc) verifyNightlyRows(ctx context.Context, rows []*ent.PublishDesired) []nightlyVerifyResult {
	out := make([]nightlyVerifyResult, 0, len(rows))
	for _, row := range rows {
		result := s.verifyNightlyRow(ctx, row)
		if s.metrics != nil {
			mirror, _ := s.mirrorForTarget(row.Target)
			s.metrics.recordVerify(mirror, result.Result)
		}
		out = append(out, result)
	}
	return out
}

// legacyPublished is the pre-verification judgement. Nightly versions are
// constant, so this only stays correct for rows that predate the file digest.
func legacyPublished(row *ent.PublishDesired) bool {
	return row.LastSucceededVersion != "" && row.LastSucceededVersion == row.DesiredVersion
}

// desiredFileSHA256 returns the tarball sha256 the row wants published.
func desiredFileSHA256(row *ent.PublishDesired) string {
	payload, err := decodeTiUPStepPayload(row.DesiredPayload)
	if err != nil {
		return ""
	}
	return payload.FileDigest
}

// mirrorForTarget maps a publish target (tiup-staging) onto its mirror name
// (staging) and base URL.
func (s *publishsrvc) mirrorForTarget(target string) (string, string) {
	mirror := strings.TrimPrefix(target, "tiup-")
	urls, err := s.mirrorURLs()
	if err != nil {
		return mirror, ""
	}
	return mirror, urls[mirror]
}
