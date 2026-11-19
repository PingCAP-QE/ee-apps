package delivery

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/dl/internal/share"
	"github.com/opencontainers/go-digest"
)

type testStore struct {
	mu      sync.Mutex
	grants  map[string]*share.Grant
	enabled bool
	events  []string
}

func (s *testStore) IssuerEnabled(context.Context, string) (bool, error) { return s.enabled, nil }
func (s *testStore) Create(_ context.Context, g *share.Grant, ttl int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	g.CreatedAt = time.Now()
	g.ExpiresAt = g.CreatedAt.Add(time.Duration(ttl) * time.Second)
	s.grants[g.ID] = g
	return nil
}
func (s *testStore) Get(_ context.Context, id, issuer string) (*share.Grant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.grants[id]
	if !ok || g.IssuedBy != issuer {
		return nil, share.ErrNotFound
	}
	c := *g
	return &c, nil
}
func (s *testStore) Revoke(_ context.Context, id, issuer string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.grants[id]
	if !ok || g.IssuedBy != issuer {
		return share.ErrNotFound
	}
	now := time.Now()
	g.RevokedAt = &now
	return nil
}
func (s *testStore) Authorize(_ context.Context, hash []byte, reserve bool) (*share.Grant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, g := range s.grants {
		if bytes.Equal(g.TokenHash, hash) && s.enabled && g.RevokedAt == nil && g.ExpiresAt.After(time.Now()) && (g.MaxSessions == nil || g.SessionsStarted < *g.MaxSessions) {
			if reserve {
				g.SessionsStarted++
			}
			c := *g
			return &c, nil
		}
	}
	return nil, share.ErrNotFound
}
func (s *testStore) Event(_ context.Context, _ string, phase, outcome string, _ int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, phase+"/"+outcome)
	return nil
}

type testBackend struct {
	content      string
	blobs        map[string]string
	fetched      []share.Artifact
	resolveCalls int
	err          error
	reader       func() io.ReadCloser
}

func (b *testBackend) Resolve(_ context.Context, repo, tag, file string) (share.Artifact, error) {
	b.resolveCalls++
	if b.err != nil {
		return share.Artifact{}, b.err
	}
	d := digest.FromString(b.content).String()
	b.blobs[d] = b.content
	return share.Artifact{Repository: repo, SourceTag: tag, Filename: file, ManifestDigest: d, BlobDigest: d, BlobSize: int64(len(b.content)), MediaType: "application/octet-stream"}, nil
}
func (b *testBackend) Fetch(_ context.Context, a share.Artifact) (io.ReadCloser, error) {
	b.fetched = append(b.fetched, a)
	if b.err != nil {
		return nil, b.err
	}
	if b.reader != nil {
		return b.reader(), nil
	}
	return io.NopCloser(strings.NewReader(b.blobs[a.BlobDigest])), nil
}

type fixture struct {
	s                  *share.Service
	store              *testStore
	backend            *testBackend
	logs               *bytes.Buffer
	management, public http.Handler
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	hash := sha256.Sum256([]byte("issuer-secret"))
	other := sha256.Sum256([]byte("other-secret"))
	cfg := &share.Config{PublicURL: "https://downloads.example.com", Issuers: []share.Policy{
		{ID: "issuer", TokenSHA256: hex.EncodeToString(hash[:]), Repositories: []string{"registry.example.com/releases/"}, AllowedTags: `v[0-9]+\.[0-9]+\.[0-9]+`, MaxTTLSeconds: 86400},
		{ID: "other", TokenSHA256: hex.EncodeToString(other[:]), Repositories: []string{"registry.example.com/releases/"}, AllowedTags: `v[0-9]+\.[0-9]+\.[0-9]+`, MaxTTLSeconds: 86400},
	}}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	f := &fixture{store: &testStore{grants: map[string]*share.Grant{}, enabled: true}, backend: &testBackend{content: "original artifact", blobs: map[string]string{}}, logs: new(bytes.Buffer)}
	f.s = &share.Service{Store: f.store, Backend: f.backend, Config: cfg, Logger: log.New(f.logs, "", 0)}
	limits := Limits{Concurrent: 32, RequestsPerSecond: 100000, Burst: 100000, TransferTimeout: time.Minute}
	f.management = Management(f.s, limits)
	f.public = Public(f.s, limits)
	return f
}

const createBody = `{"artifact":{"repository":"registry.example.com/releases/tidb","tag":"v8.1.0","file":"tidb.tar.gz"},"purpose":"customer-delivery","customer_id":"customer-1"}`

func request(h http.Handler, method, path, body, secret string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	if secret != "" {
		r.Header.Set("Authorization", "Bearer "+secret)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func (f *fixture) issue(t *testing.T) (id, path, token string) {
	t.Helper()
	w := request(f.management, "POST", "/api/v1/shares", createBody, "issuer-secret")
	if w.Code != 201 {
		t.Fatalf("create: %d %s", w.Code, w.Body.String())
	}
	var res struct {
		ID  string `json:"id"`
		URL string `json:"download_url"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	path = strings.TrimPrefix(res.URL, "https://downloads.example.com")
	return res.ID, path, strings.TrimPrefix(path, "/d/")
}

func TestIssueRedeemPinnedBlobAndReturnSecretOnlyOnce(t *testing.T) {
	f := newFixture(t)
	id, path, token := f.issue(t)
	if len(token) != 43 {
		t.Fatal("token lacks 256 bits")
	}
	g := f.store.grants[id]
	h := sha256.Sum256([]byte(token))
	if !bytes.Equal(g.TokenHash, h[:]) {
		t.Fatal("token hash mismatch")
	}
	f.backend.content = "replacement tag contents"
	w := request(f.public, "GET", path, "", "")
	if w.Code != 200 || w.Body.String() != "original artifact" {
		t.Fatalf("unpinned download: %d %s", w.Code, w.Body.String())
	}
	for k, want := range map[string]string{"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Content-Type": "application/octet-stream"} {
		if w.Header().Get(k) != want {
			t.Fatalf("missing %s", k)
		}
	}
	if !strings.HasPrefix(w.Header().Get("Content-Disposition"), "attachment;") {
		t.Fatal("missing attachment header")
	}
	w = request(f.management, "GET", "/api/v1/shares/"+id, "", "issuer-secret")
	if w.Code != 200 || strings.Contains(w.Body.String(), token) || strings.Contains(w.Body.String(), "download_url") {
		t.Fatal("secret returned again")
	}
	if len(f.backend.fetched) != 1 || f.backend.fetched[0].BlobDigest != g.BlobDigest {
		t.Fatal("fetch did not use pinned grant")
	}
	if strings.Contains(f.logs.String(), token) || strings.Contains(f.logs.String(), "issuer-secret") || strings.Contains(f.logs.String(), "registry.example.com") {
		t.Fatal("sensitive logging")
	}
	metrics := httptest.NewRecorder()
	f.s.Metrics.ServeHTTP(metrics, httptest.NewRequest("GET", "/metrics", nil))
	if !strings.Contains(metrics.Body.String(), "dl_delivery_active_transfers 0") || strings.Contains(metrics.Body.String(), token) {
		t.Fatal("unsafe metrics")
	}
}

func TestPublicDeniesEveryOtherSurfaceAndParameterSubstitution(t *testing.T) {
	f := newFixture(t)
	_, path, _ := f.issue(t)
	for _, target := range []string{"/oci-file/registry.example.com/releases/tidb?tag=v8.1.0&file=tidb.tar.gz", "/oci-files/repo", "/oci-file-sha256/repo", "/s3-obj/bucket/key", "/gcs-obj/bucket/key", "/api/v1/shares", "/healthz", "/livez", "/metrics", "/dl" + path, "/d/../oci-files/repo", "//d/anything", path + "/", path + "?file=other", path + "?repository=evil", path + "?", strings.Replace(path, "/d/", "/%64/", 1)} {
		for _, method := range []string{"GET", "HEAD", "POST"} {
			w := request(f.public, method, target, "", "")
			if w.Code != 404 {
				t.Errorf("%s %s: %d", method, target, w.Code)
			}
		}
	}
	if len(f.backend.fetched) != 0 {
		t.Fatal("disallowed request accessed registry")
	}
	for _, path := range []string{path, "/oci-file/repo", "/healthz"} {
		if w := request(f.management, "GET", path, "", ""); w.Code != 404 {
			t.Errorf("management leaked route %s: %d", path, w.Code)
		}
	}
}

func TestExpiryRevocationIssuerDisableAndHEAD(t *testing.T) {
	for _, state := range []string{"expired", "revoked", "disabled", "unknown"} {
		t.Run(state, func(t *testing.T) {
			f := newFixture(t)
			id, path, _ := f.issue(t)
			head := request(f.public, "HEAD", path, "", "")
			if head.Code != 200 || head.Header().Get("Content-Length") != "17" || len(f.backend.fetched) != 0 || f.store.grants[id].SessionsStarted != 0 {
				t.Fatal("HEAD fetched a blob or consumed quota")
			}
			switch state {
			case "expired":
				f.store.grants[id].ExpiresAt = time.Now().Add(-time.Second)
			case "revoked":
				w := request(f.management, "DELETE", "/api/v1/shares/"+id, "", "issuer-secret")
				if w.Code != 204 {
					t.Fatal(w.Body.String())
				}
			case "disabled":
				f.store.enabled = false
			case "unknown":
				path = "/d/" + strings.Repeat("A", 43)
			}
			for _, method := range []string{"GET", "HEAD"} {
				w := request(f.public, method, path, "", "")
				if w.Code != 404 || w.Header().Get("Content-Disposition") != "" {
					t.Fatalf("%s accepted %s token", method, state)
				}
			}
			if len(f.backend.fetched) != 0 {
				t.Fatal("denied token accessed registry")
			}
		})
	}
}

func TestIssuerAuthorizationAndOwnership(t *testing.T) {
	f := newFixture(t)
	if w := request(f.management, "POST", "/api/v1/shares", createBody, ""); w.Code != 401 {
		t.Fatal("missing auth accepted")
	}
	if w := request(f.management, "POST", "/api/v1/shares", createBody, "wrong"); w.Code != 401 {
		t.Fatal("wrong auth accepted")
	}
	for _, body := range []string{strings.ReplaceAll(createBody, "releases/tidb", "releases-evil/tidb"), strings.ReplaceAll(createBody, "v8.1.0", "latest")} {
		w := request(f.management, "POST", "/api/v1/shares", body, "issuer-secret")
		if w.Code != 403 {
			t.Fatalf("unauthorized scope: %d", w.Code)
		}
	}
	body := strings.Replace(createBody, `"purpose":`, `"expires_in_seconds":86401,"purpose":`, 1)
	if w := request(f.management, "POST", "/api/v1/shares", body, "issuer-secret"); w.Code != 400 {
		t.Fatal("excess TTL accepted")
	}
	if f.backend.resolveCalls != 0 {
		t.Fatal("denied issuer reached registry")
	}
	id, _, _ := f.issue(t)
	for _, method := range []string{"GET", "DELETE"} {
		if w := request(f.management, method, "/api/v1/shares/"+id, "", "other-secret"); w.Code != 404 {
			t.Fatal("cross-issuer ownership bypass")
		}
	}
}

func TestUpstreamFailureIsSanitized(t *testing.T) {
	f := newFixture(t)
	_, path, token := f.issue(t)
	f.backend.err = errors.New("https://private.registry.example.com password=do-not-log")
	w := request(f.public, "GET", path, "", "")
	if w.Code != 502 || strings.Contains(w.Body.String(), "private.registry") || strings.Contains(f.logs.String(), "do-not-log") || strings.Contains(f.logs.String(), token) {
		t.Fatal("upstream detail leaked")
	}
}

type brokenReader struct{}

func (brokenReader) Read([]byte) (int, error) { return 0, errors.New("private TLS detail") }
func (brokenReader) Close() error             { return nil }
func TestFailedStreamAbortsAndAudits(t *testing.T) {
	f := newFixture(t)
	_, path, _ := f.issue(t)
	f.backend.reader = func() io.ReadCloser { return brokenReader{} }
	defer func() {
		if recover() != http.ErrAbortHandler {
			t.Error("failed transfer did not abort HTTP stream")
		}
		if !strings.Contains(f.logs.String(), "stream_failed") || strings.Contains(f.logs.String(), "private TLS") {
			t.Error("incorrect transfer audit")
		}
		if f.s.Metrics.Active.Load() != 0 {
			t.Error("active transfer leaked")
		}
	}()
	request(f.public, "GET", path, "", "")
}

type boundedReader struct {
	remaining int64
	maxRead   int
}

func (r *boundedReader) Read(p []byte) (int, error) {
	if len(p) > r.maxRead {
		r.maxRead = len(p)
	}
	if r.remaining == 0 {
		return 0, io.EOF
	}
	n := int(min(int64(len(p)), r.remaining))
	r.remaining -= int64(n)
	return n, nil
}
func (*boundedReader) Close() error { return nil }

type countingResponse struct {
	header http.Header
	bytes  int64
}

func (w *countingResponse) Header() http.Header         { return w.header }
func (*countingResponse) WriteHeader(int)               {}
func (w *countingResponse) Write(p []byte) (int, error) { w.bytes += int64(len(p)); return len(p), nil }
func TestMultiGiBTransferUsesBoundedReads(t *testing.T) {
	f := newFixture(t)
	id, path, _ := f.issue(t)
	const size = int64(5) * 1024 * 1024 * 1024
	f.store.grants[id].BlobSize = size
	rc := &boundedReader{remaining: size}
	f.backend.reader = func() io.ReadCloser { return rc }
	w := &countingResponse{header: make(http.Header)}
	f.public.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
	if w.bytes != size || rc.maxRead > 32*1024 {
		t.Fatalf("unbounded stream: bytes=%d max read=%d", w.bytes, rc.maxRead)
	}
}

func TestRequestLimitsBoundConcurrencyAndRate(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	h := limited(Limits{Concurrent: 1, RequestsPerSecond: 1000, Burst: 100}, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/blocked" {
			close(entered)
			<-release
		}
		w.WriteHeader(http.StatusOK)
	}))
	done := make(chan struct{})
	go func() { defer close(done); request(h, "GET", "/blocked", "", "") }()
	<-entered
	if w := request(h, "GET", "/second", "", ""); w.Code != 503 {
		t.Error("concurrent request was not rejected")
	}
	close(release)
	<-done
	if w := request(h, "GET", "/after", "", ""); w.Code != 200 {
		t.Fatal("concurrency slot leaked")
	}
	h = limited(Limits{Concurrent: 1, RequestsPerSecond: 0.000001, Burst: 1}, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) }))
	if w := request(h, "GET", "/first", "", ""); w.Code != 200 {
		t.Fatal("first request rate limited")
	}
	if w := request(h, "GET", "/next", "", ""); w.Code != 429 || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("rate limit not enforced")
	}
}

func TestAnonymousBodyAndNonBearerCredentialsRejected(t *testing.T) {
	f := newFixture(t)
	_, path, _ := f.issue(t)
	if w := request(f.public, "GET", path, "untrusted body", ""); w.Code != 404 || w.Header().Get("Connection") != "close" {
		t.Fatal("anonymous body accepted")
	}
	r := httptest.NewRequest("POST", "/api/v1/shares", strings.NewReader(createBody))
	r.Header.Set("Authorization", "Basic issuer-secret")
	w := httptest.NewRecorder()
	f.management.ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatal("non-bearer credentials accepted")
	}
}
