package web

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/auth"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type fakeAuth struct {
	identity *auth.Identity
	err      error
}

func (f fakeAuth) Validate(context.Context, string) (*auth.Identity, error) {
	return f.identity, f.err
}

type fakeStore struct {
	mu            sync.Mutex
	cards         map[string][]store.ApprovalCard
	requests      map[string]*store.Request
	pending       []*store.Request
	pendingCount  int
	recentCount   int
	created       *store.Request
	transitions   []string
	getErr        error
	transitionErr error
}

func newFakeStore() *fakeStore {
	return &fakeStore{requests: map[string]*store.Request{}, cards: map[string][]store.ApprovalCard{}}
}

func (f *fakeStore) Ready(context.Context) error { return nil }
func (f *fakeStore) CreateRequest(_ context.Context, req *store.Request) error {
	if req.ID == "" {
		req.ID = "req-1"
	}
	f.created = req
	f.requests[req.ID] = req
	return nil
}
func (f *fakeStore) GetRequest(_ context.Context, id string) (*store.Request, error) {
	if f.getErr != nil {
		return nil, f.getErr
	}
	req, ok := f.requests[id]
	if !ok {
		return nil, store.ErrNotFound
	}
	return req, nil
}
func (f *fakeStore) ListByRequester(context.Context, string, int) ([]*store.Request, error) {
	out := make([]*store.Request, 0, len(f.requests))
	for _, req := range f.requests {
		out = append(out, req)
	}
	return out, nil
}
func (f *fakeStore) CountPendingByRequester(context.Context, string) (int, error) {
	return f.pendingCount, nil
}
func (f *fakeStore) CountRequestsSince(context.Context, string, time.Time) (int, error) {
	return f.recentCount, nil
}
func (f *fakeStore) ListExpired(context.Context, time.Time, int) ([]*store.Request, error) {
	return nil, nil
}
func (f *fakeStore) ListPending(context.Context, int) ([]*store.Request, error) {
	return f.pending, nil
}
func (f *fakeStore) Transition(_ context.Context, id string, from, to store.Status, actor, note string, _ time.Time) (bool, error) {
	req, ok := f.requests[id]
	if !ok || req.Status != from {
		return false, nil
	}
	req.Status = to
	if f.transitionErr != nil {
		return false, f.transitionErr
	}
	req.ApproverEmail = actor
	req.DecisionNote = note
	f.transitions = append(f.transitions, id+"->"+string(to))
	return true, nil
}
func (f *fakeStore) SetDelivery(context.Context, string, store.DeliveryStatus, int, string) error {
	return nil
}
func (f *fakeStore) AppendEvent(context.Context, *store.Event) error            { return nil }
func (f *fakeStore) SetFileShare(context.Context, string, string, []byte) error { return nil }
func (f *fakeStore) SaveFileCiphertext(context.Context, string, []byte) error   { return nil }
func (f *fakeStore) ClearFileCiphertext(context.Context, string) error          { return nil }
func (f *fakeStore) SaveApprovalCards(_ context.Context, requestID string, cards []store.ApprovalCard) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cards[requestID] = append(f.cards[requestID], cards...)
	return nil
}

func (f *fakeStore) ListApprovalCards(_ context.Context, requestID string) ([]store.ApprovalCard, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]store.ApprovalCard(nil), f.cards[requestID]...), nil
}

func (f *fakeStore) Close() error { return nil }

func testConfig(t *testing.T) *config.Manager {
	t.Helper()
	cfg := config.Default()
	cfg.PublicURL = "https://approvals.example.com"
	cfg.DLBaseURL = "http://dl-management:80"
	cfg.OIDCClientID = "client-id.apps.googleusercontent.com"
	cfg.AllowedEmailDomains = []string{"example.com"}
	cfg.ApproverEmails = []string{"approver@example.com"}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	return config.NewManager(cfg)
}

func testHandler(t *testing.T, st *fakeStore, identity *auth.Identity) http.Handler {
	t.Helper()
	return New(Options{
		Store:   st,
		Config:  testConfig(t),
		Auth:    fakeAuth{identity: identity},
		Metrics: &metrics.Metrics{},
	})
}

func requestWithToken(method, target string, body io.Reader) *http.Request {
	r := httptest.NewRequest(method, target, body)
	r.Header.Set("X-Approval-Id-Token", "token")
	return r
}

var csrfPattern = regexp.MustCompile(`name="csrf" value="([^"]+)"`)

func TestFormRequiresIdentityAndRendersCSRF(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com", Subject: "sub"})

	r := httptest.NewRequest(http.MethodGet, "/", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("missing token = %d, want 401", w.Code)
	}

	w = httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("form = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if got := w.Header().Get("Content-Security-Policy"); !strings.Contains(got, "default-src 'self'") {
		t.Fatalf("CSP = %q", got)
	}
	if strings.Contains(w.Body.String(), "<script") {
		t.Fatalf("form must not contain inline scripts")
	}
	m := csrfPattern.FindStringSubmatch(w.Body.String())
	if len(m) != 2 || m[1] == "" {
		t.Fatalf("CSRF token missing from form: %s", w.Body.String())
	}
	if len(w.Result().Cookies()) == 0 {
		t.Fatal("CSRF cookie not set")
	}
}

func TestPagesUsePublicURLPathPrefix(t *testing.T) {
	// The gateway publishes the pages under /dl/grants and rewrites the prefix
	// away, so every link and redirect must rebuild it. Without this the
	// browser posts to /requests on the parent host and another app answers.
	cfg := config.Default()
	cfg.PublicURL = "https://prow.tidb.net/dl/grants"
	cfg.DLBaseURL = "http://dl-management:80"
	cfg.OIDCClientID = "client-id.apps.googleusercontent.com"
	cfg.AllowedEmailDomains = []string{"example.com"}
	cfg.ApproverEmails = []string{"approver@example.com"}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	st := newFakeStore()
	h := New(Options{
		Store:   st,
		Config:  config.NewManager(cfg),
		Auth:    fakeAuth{identity: &auth.Identity{Email: "requester@example.com", Subject: "sub"}},
		Metrics: &metrics.Metrics{},
	})

	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"us-docker.pkg.dev/project/hub/package v8.1.0 tidb.tar.gz"},
		"ttl":     {"3600"},
		"csrf":    {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, body=%s", w.Code, w.Body.String())
	}
	if got := w.Header().Get("Location"); got != "/dl/grants/requests/req-1" {
		t.Fatalf("redirect location = %q, want the published prefix", got)
	}

	w = httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/", nil))
	body := w.Body.String()
	if !strings.Contains(body, `action="/dl/grants/requests"`) {
		t.Fatalf("form action missing the published prefix: %s", body)
	}
	if !strings.Contains(body, `href="/dl/grants/requests"`) {
		t.Fatalf("links missing the published prefix: %s", body)
	}
}

func TestFormPrefillsFromQuery(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	// Mirrors a legacy /dl/oci-file link: repository in the path, file/tag in
	// the query. Repeated `file` params and a comma list are both accepted and
	// become one target line each.
	target := "/?repository=us-docker.pkg.dev/project/hub/package&tag=v8.5.4" +
		"&file=tidb.tar.gz&file=tikv.tar.gz&files=tiflash.tar.gz,tools.tar.gz" +
		"&ttl=7d&note=customer%20asked"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, target, nil))
	if w.Code != http.StatusOK {
		t.Fatalf("form = %d", w.Code)
	}
	body := w.Body.String()
	for _, want := range []string{
		`name="targets"`,
		"us-docker.pkg.dev/project/hub/package v8.5.4 tidb.tar.gz",
		"us-docker.pkg.dev/project/hub/package v8.5.4 tikv.tar.gz",
		"us-docker.pkg.dev/project/hub/package v8.5.4 tiflash.tar.gz",
		"us-docker.pkg.dev/project/hub/package v8.5.4 tools.tar.gz",
		`customer asked`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("prefill missing %q", want)
		}
	}
	if !strings.Contains(body, `<option value="24h" selected>`) {
		t.Fatalf("TTL must keep the default and ignore the query: %s", body)
	}
	if strings.Contains(body, `value="7d"`) {
		t.Fatal("TTL must not be prefilled from the query")
	}
}

func TestCreateAcceptsMultipleRepositories(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	csrf, cookie := obtainCSRF(t, h)
	// Two repositories in one request, and a repeated triple that must collapse
	// instead of tripping the per-request unique index.
	form := url.Values{
		"targets": {
			"us-docker.pkg.dev/project/hub/package v8.1.0 tidb.tar.gz\n" +
				"us-docker.pkg.dev/project/hotfix/package v8.1.0-hotfix tikv.tar.gz\n" +
				"us-docker.pkg.dev/project/hub/package v8.1.0 tidb.tar.gz",
		},
		"ttl":  {"3600"},
		"note": {"two repositories"},
		"csrf": {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, body=%s", w.Code, w.Body.String())
	}
	want := []store.File{
		{Repository: "us-docker.pkg.dev/project/hub/package", Tag: "v8.1.0", Filename: "tidb.tar.gz"},
		{Repository: "us-docker.pkg.dev/project/hotfix/package", Tag: "v8.1.0-hotfix", Filename: "tikv.tar.gz"},
	}
	if st.created == nil {
		t.Fatal("request was not created")
	}
	if len(st.created.Files) != len(want) {
		t.Fatalf("files = %+v, want %d", st.created.Files, len(want))
	}
	for i, file := range st.created.Files {
		if file.Repository != want[i].Repository || file.Tag != want[i].Tag || file.Filename != want[i].Filename {
			t.Fatalf("file %d = %+v, want %+v", i, file, want[i])
		}
	}
}

func TestParseTargets(t *testing.T) {
	tooMany := make([]string, 0, 31)
	for i := 0; i < 31; i++ {
		tooMany = append(tooMany, fmt.Sprintf("r.example.com/a v1 file-%02d.tar.gz", i))
	}
	cases := []struct {
		name    string
		in      string
		want    int
		wantErr string
	}{
		{"tab separated", "r.example.com/a\tv1\ta.tar.gz", 1, ""},
		{"blank lines", "\n\nr.example.com/a v1 a.tar.gz\n\n", 1, ""},
		{"wrong field count", "r.example.com/a v1", 0, "line 1: use <repository> <tag> <filename>"},
		{"filename with a space", "r.example.com/a v1 a b.tar.gz", 0, "line 1: use <repository> <tag> <filename>"},
		{"invalid repository", "../evil v1 a.tar.gz", 0, "line 1: invalid repository"},
		{"invalid tag", "r.example.com/a v1@bad a.tar.gz", 0, "line 1: invalid tag"},
		{"filename with a slash", "r.example.com/a v1 ../secret", 0, "line 1: invalid filename"},
		{"second line", "r.example.com/a v1 a.tar.gz\nr.example.com/b v1", 0, "line 2: use <repository> <tag> <filename>"},
		{"nothing", "  \n \n", 0, "at least one target is required"},
		{"too many", strings.Join(tooMany, "\n"), 0, "provide 1-30 targets"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseTargets(tc.in)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("parseTargets(%q) error = %v, want %q", tc.in, err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("parseTargets(%q) error: %v", tc.in, err)
			}
			if len(got) != tc.want {
				t.Fatalf("parseTargets(%q) = %+v, want %d targets", tc.in, got, tc.want)
			}
		})
	}
}

func TestCreateKeepsTargetsAfterRejection(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"r.example.com/a v1 a.tar.gz\nr.example.com/b v2"},
		"ttl":     {"60"},
		"csrf":    {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	body := w.Body.String()
	if w.Code != http.StatusOK || !strings.Contains(body, "line 2: use &lt;repository&gt; &lt;tag&gt; &lt;filename&gt;") {
		t.Fatalf("create = %d, body=%s", w.Code, body)
	}
	if !strings.Contains(body, "r.example.com/a v1 a.tar.gz") {
		t.Fatalf("typed targets were dropped: %s", body)
	}
	if st.created != nil {
		t.Fatal("an invalid form must not create a request")
	}
}

func TestParseTTL(t *testing.T) {
	cases := []struct {
		in      string
		want    int64
		wantErr bool
	}{
		{"86400", 86400, false},
		{"24h", 86400, false},
		{"168h", 604800, false},
		{"1m", 60, false},
		{"30s", 30, false},
		{"2h30m", 9000, false},
		{" 30s ", 30, false},
		{"7d", 0, true},
		{"1w", 0, true},
		{"", 0, true},
		{"abc", 0, true},
	}
	for _, tc := range cases {
		got, err := parseTTL(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Fatalf("parseTTL(%q) = %d, want error", tc.in, got)
			}
			continue
		}
		if err != nil {
			t.Fatalf("parseTTL(%q) error: %v", tc.in, err)
		}
		if got != tc.want {
			t.Fatalf("parseTTL(%q) = %d, want %d", tc.in, got, tc.want)
		}
	}
}

func TestFormAcceptsFriendlyTTL(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"r.example.com/a v1 a.tar.gz"},
		"ttl":     {"2h"},
		"csrf":    {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, body=%s", w.Code, w.Body.String())
	}
	if st.created == nil || st.created.TTLSeconds != 7200 {
		t.Fatalf("ttl = %+v", st.created)
	}
}

func TestForbiddenIdentity(t *testing.T) {
	h := New(Options{Store: newFakeStore(), Config: testConfig(t), Auth: fakeAuth{err: auth.ErrForbidden}, Metrics: &metrics.Metrics{}})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("forbidden = %d, want 403", w.Code)
	}
}

func TestCreateRequest(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})

	form := url.Values{
		"targets": {
			"us-docker.pkg.dev/project/hub/package v8.1.0_linux_amd64 tidb.tar.gz\n" +
				"us-docker.pkg.dev/project/hub/package v8.1.0_linux_amd64 tikv.tar.gz",
		},
		"ttl":  {"3600"},
		"note": {"customer asked"},
	}
	csrf, cookie := obtainCSRF(t, h)
	form.Set("csrf", csrf)

	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, want 303; body=%s", w.Code, w.Body.String())
	}
	if got := w.Header().Get("Location"); got != "/requests/req-1" {
		t.Fatalf("location = %q", got)
	}
	if st.created == nil || st.created.RequesterEmail != "requester@example.com" || st.created.Status != store.StatusPending {
		t.Fatalf("created request = %+v", st.created)
	}
	if st.created.TTLSeconds != 3600 || len(st.created.Files) != 2 {
		t.Fatalf("created request = %+v", st.created)
	}
}

func TestCreateRejectsBadCSRFAndInput(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com"})
	valid := url.Values{"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"60"}}

	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(valid.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("missing CSRF = %d, want 403", w.Code)
	}

	csrf, cookie := obtainCSRF(t, h)
	bad := url.Values{"targets": {"../evil v1 a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf}}
	r = requestWithToken(http.MethodPost, "/requests", strings.NewReader(bad.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "invalid repository") {
		t.Fatalf("bad repo = %d, body=%s", w.Code, w.Body.String())
	}
}

func TestRateLimits(t *testing.T) {
	st := newFakeStore()
	st.pendingCount = 5
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf}}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("pending limit = %d, want 429", w.Code)
	}
}

func TestDetailOwnership(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "other@example.com", Status: store.StatusPending}
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})

	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/req-1", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("other user detail = %d, want 404", w.Code)
	}

	approver := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	w = httptest.NewRecorder()
	approver.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/req-1", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("approver detail = %d, want 200", w.Code)
	}
	if !strings.Contains(w.Body.String(), "other@example.com") {
		t.Fatalf("detail body missing requester: %s", w.Body.String())
	}
}

func obtainCSRF(t *testing.T, h http.Handler) (string, *http.Cookie) {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("form = %d", w.Code)
	}
	m := csrfPattern.FindStringSubmatch(w.Body.String())
	if len(m) != 2 {
		t.Fatalf("CSRF token missing")
	}
	cookies := w.Result().Cookies()
	if len(cookies) == 0 {
		t.Fatal("CSRF cookie missing")
	}
	return m[1], cookies[0]
}

func TestListShowsOwnAndPending(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	st.pending = []*store.Request{{ID: "req-2", RequesterEmail: "other@example.com", Status: store.StatusPending}}

	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests", nil))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "req-1") {
		t.Fatalf("list = %d, body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "req-2") {
		t.Fatalf("non-approver saw another requester: %s", w.Body.String())
	}

	approver := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	w = httptest.NewRecorder()
	approver.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests", nil))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "req-2") {
		t.Fatalf("approver list = %d, body=%s", w.Code, w.Body.String())
	}
}

func TestDetailNotFound(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com"})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/missing", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("missing detail = %d, want 404", w.Code)
	}
}

func TestInvalidFilenameAndTTL(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com"})
	csrf, cookie := obtainCSRF(t, h)
	tests := []struct {
		name string
		form url.Values
		want string
	}{
		{"filename", url.Values{"targets": {"r.example.com/a v1 ../secret"}, "ttl": {"60"}}, "invalid filename"},
		{"ttl", url.Values{"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"0"}}, "TTL must be"},
		{"too many", url.Values{"targets": {manyTargets(31)}, "ttl": {"60"}}, "provide 1-30 targets"},
		{"long note", url.Values{"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"60"}, "note": {strings.Repeat("x", 1025)}}, "note is too long"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tt.form.Set("csrf", csrf)
			r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(tt.form.Encode()))
			r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			r.AddCookie(cookie)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), tt.want) {
				t.Fatalf("%s = %d, body=%s", tt.name, w.Code, w.Body.String())
			}
		})
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := testHandler(t, newFakeStore(), &auth.Identity{Email: "requester@example.com"})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodPost, "/", strings.NewReader("")))
	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST / = %d, want 405", w.Code)
	}
}

type fakeRevoker struct {
	shares []string
}

func (f *fakeRevoker) RevokeShare(_ context.Context, shareID string) error {
	f.shares = append(f.shares, shareID)
	return nil
}

func postDecision(t *testing.T, h http.Handler, id, action, note string, identity *auth.Identity) *httptest.ResponseRecorder {
	t.Helper()
	csrf, cookie := obtainCSRFFor(t, h)
	form := url.Values{"csrf": {csrf}, "note": {note}}
	r := requestWithToken(http.MethodPost, "/requests/"+id+"/"+action, strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func obtainCSRFFor(t *testing.T, h http.Handler) (string, *http.Cookie) {
	return obtainCSRF(t, h)
}

// manyTargets builds a target block with n distinct lines.
func manyTargets(n int) string {
	lines := make([]string, 0, n)
	for i := 0; i < n; i++ {
		lines = append(lines, fmt.Sprintf("r.example.com/a v1 file-%02d.tar.gz", i))
	}
	return strings.Join(lines, "\n")
}

func TestApproveReject(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	h := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})

	w := postDecision(t, h, "req-1", "approve", "looks good", nil)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("approve = %d, body=%s", w.Code, w.Body.String())
	}
	if st.requests["req-1"].Status != store.StatusApproved {
		t.Fatalf("status = %s", st.requests["req-1"].Status)
	}
	if len(st.transitions) != 1 || st.transitions[0] != "req-1->approved" {
		t.Fatalf("transitions = %v", st.transitions)
	}

	// A second decision (double submit or a slower approver) is refused, but the
	// caller is sent to the detail page instead of a bare conflict page.
	w = postDecision(t, h, "req-1", "reject", "too late", nil)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("second decision = %d, want 303", w.Code)
	}
	if loc := w.Header().Get("Location"); !strings.Contains(loc, "notice=already-decided") {
		t.Fatalf("location = %q", loc)
	}
}

func TestApprovalAuthorization(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}

	nonApprover := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	if w := postDecision(t, nonApprover, "req-1", "approve", "", nil); w.Code != http.StatusForbidden {
		t.Fatalf("non-approver = %d, want 403", w.Code)
	}

	st.requests["req-1"].RequesterEmail = "approver@example.com"
	self := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	if w := postDecision(t, self, "req-1", "approve", "", nil); w.Code != http.StatusForbidden {
		t.Fatalf("self approval = %d, want 403", w.Code)
	}
}

func TestRevoke(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	if w := postDecision(t, h, "req-1", "revoke", "", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("revoke = %d, body=%s", w.Code, w.Body.String())
	}
	if st.requests["req-1"].Status != store.StatusRevoked {
		t.Fatalf("status = %s", st.requests["req-1"].Status)
	}

	st.requests["req-2"] = &store.Request{ID: "req-2", RequesterEmail: "other@example.com", Status: store.StatusPending}
	if w := postDecision(t, h, "req-2", "revoke", "", nil); w.Code != http.StatusNotFound {
		t.Fatalf("other revoke = %d, want 404", w.Code)
	}
}

func TestRevokeApprovedCallsRevoker(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusApproved,
		Files: []store.File{{ShareID: "share-1"}, {ShareID: ""}}}
	revoker := &fakeRevoker{}
	h := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "requester@example.com"}}, Revoker: revoker, Metrics: &metrics.Metrics{}})
	if w := postDecision(t, h, "req-1", "revoke", "", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("revoke approved = %d, body=%s", w.Code, w.Body.String())
	}
	if len(revoker.shares) != 1 || revoker.shares[0] != "share-1" {
		t.Fatalf("revoked shares = %v", revoker.shares)
	}
}

func TestPendingPageRequiresApprover(t *testing.T) {
	st := newFakeStore()
	st.pending = []*store.Request{{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}}

	requester := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	w := httptest.NewRecorder()
	requester.ServeHTTP(w, requestWithToken(http.MethodGet, "/pending", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("requester pending = %d, want 403", w.Code)
	}

	approver := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	w = httptest.NewRecorder()
	approver.ServeHTTP(w, requestWithToken(http.MethodGet, "/pending", nil))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "req-1") {
		t.Fatalf("approver pending = %d, body=%s", w.Code, w.Body.String())
	}
}

func TestDecisionStoreError(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	st.transitionErr = errors.New("db down")
	h := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	if w := postDecision(t, h, "req-1", "approve", "", nil); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("decision store error = %d, want 503", w.Code)
	}
}

func TestDetailRendersDecisionForms(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}

	approver := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	w := httptest.NewRecorder()
	approver.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/req-1", nil))
	body := w.Body.String()
	if w.Code != http.StatusOK || !strings.Contains(body, `/requests/req-1/approve`) || !strings.Contains(body, `/requests/req-1/reject`) || !strings.Contains(body, `/requests/req-1/revoke`) {
		t.Fatalf("approver detail = %d, body=%s", w.Code, body)
	}

	requester := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	w = httptest.NewRecorder()
	requester.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/req-1", nil))
	body = w.Body.String()
	if w.Code != http.StatusOK || strings.Contains(body, `/requests/req-1/approve`) || !strings.Contains(body, `/requests/req-1/revoke`) {
		t.Fatalf("requester detail = %d, body=%s", w.Code, body)
	}
}

func TestDecisionAndRevokeEdges(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "approver@example.com"})
	if w := postDecision(t, h, "missing", "approve", "", nil); w.Code != http.StatusNotFound {
		t.Fatalf("missing approve = %d, want 404", w.Code)
	}

	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	if w := postDecision(t, h, "req-1", "approve", strings.Repeat("x", 1025), nil); w.Code != http.StatusBadRequest {
		t.Fatalf("long note = %d, want 400", w.Code)
	}

	st.requests["req-2"] = &store.Request{ID: "req-2", RequesterEmail: "requester@example.com", Status: store.StatusRejected}
	requester := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	if w := postDecision(t, requester, "req-2", "revoke", "", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("revoke rejected = %d, want 303", w.Code)
	} else if loc := w.Header().Get("Location"); !strings.Contains(loc, "notice=not-revocable") {
		t.Fatalf("location = %q", loc)
	}
}

func TestRevokeRejectsMissingCSRF(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	r := requestWithToken(http.MethodPost, "/requests/req-1/revoke", strings.NewReader(""))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("revoke missing csrf = %d, want 403", w.Code)
	}
}

type fakeDeliverer struct {
	ch  chan string
	err error
}

func (f *fakeDeliverer) Deliver(_ context.Context, id string) error {
	f.ch <- id
	return f.err
}

func TestApproveTriggersDelivery(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	d := &fakeDeliverer{ch: make(chan string, 1)}
	h := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "approver@example.com"}}, Delivery: d, Metrics: &metrics.Metrics{}})

	if w := postDecision(t, h, "req-1", "approve", "", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("approve = %d", w.Code)
	}
	select {
	case id := <-d.ch:
		if id != "req-1" {
			t.Fatalf("delivered id = %q", id)
		}
	case <-time.After(time.Second):
		t.Fatal("approval did not trigger delivery")
	}
}

type fakeApprovalNotifier struct {
	ch    chan string
	cards []store.ApprovalCard
	err   error
}

func (f *fakeApprovalNotifier) NotifyApproval(_ context.Context, req *store.Request) ([]store.ApprovalCard, error) {
	f.ch <- req.ID
	return f.cards, f.err
}

// fakeCardUpdater records the card patches the handler issues after a decision.
type fakeCardUpdater struct {
	mu      sync.Mutex
	updates []cardUpdate
}

type cardUpdate struct {
	requestID string
	outcome   string
	actor     string
	note      string
	cards     int
}

func (f *fakeCardUpdater) UpdateApprovalCards(_ context.Context, req *store.Request, cards []store.ApprovalCard, outcome, actor, note string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.updates = append(f.updates, cardUpdate{requestID: req.ID, outcome: outcome, actor: actor, note: note, cards: len(cards)})
	return nil
}

func (f *fakeCardUpdater) wait(t *testing.T) []cardUpdate {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		if len(f.updates) > 0 {
			out := append([]cardUpdate(nil), f.updates...)
			f.mu.Unlock()
			return out
		}
		f.mu.Unlock()
		time.Sleep(10 * time.Millisecond)
	}
	return nil
}

func TestCreateNotifiesApprover(t *testing.T) {
	st := newFakeStore()
	n := &fakeApprovalNotifier{ch: make(chan string, 1)}
	h := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "requester@example.com"}}, ApprovalNotifier: n, Metrics: &metrics.Metrics{}})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d", w.Code)
	}
	select {
	case id := <-n.ch:
		if id != "req-1" {
			t.Fatalf("notified id = %q", id)
		}
	case <-time.After(time.Second):
		t.Fatal("approval notification not triggered")
	}
}

type fakeDecisionNotifier struct {
	ch       chan string
	decision string
}

func (f *fakeDecisionNotifier) NotifyDecision(_ context.Context, _ *store.Request, decision, _ string) error {
	f.decision = decision
	f.ch <- decision
	return nil
}

func TestRejectAndRevokeNotify(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	n := &fakeDecisionNotifier{ch: make(chan string, 2)}
	h := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "approver@example.com"}}, DecisionNotifier: n, Metrics: &metrics.Metrics{}})
	if w := postDecision(t, h, "req-1", "reject", "no", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("reject = %d", w.Code)
	}
	select {
	case got := <-n.ch:
		if got != "rejected" {
			t.Fatalf("decision = %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("reject did not notify")
	}

	st.requests["req-2"] = &store.Request{ID: "req-2", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	requester := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "requester@example.com"}}, DecisionNotifier: n, Metrics: &metrics.Metrics{}})
	if w := postDecision(t, requester, "req-2", "revoke", "", nil); w.Code != http.StatusSeeOther {
		t.Fatalf("revoke = %d", w.Code)
	}
	select {
	case got := <-n.ch:
		if got != "revoked" {
			t.Fatalf("decision = %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("revoke did not notify")
	}
}

func TestCreateRecordsApprovalCards(t *testing.T) {
	st := newFakeStore()
	n := &fakeApprovalNotifier{ch: make(chan string, 1), cards: []store.ApprovalCard{{ApproverEmail: "approver@example.com", MessageID: "om-1"}}}
	h := New(Options{Store: st, Config: testConfig(t), Auth: fakeAuth{identity: &auth.Identity{Email: "requester@example.com"}}, ApprovalNotifier: n, Metrics: &metrics.Metrics{}})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"r.example.com/a v1 a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d", w.Code)
	}
	<-n.ch
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		st.mu.Lock()
		saved := append([]store.ApprovalCard(nil), st.cards["req-1"]...)
		st.mu.Unlock()
		if len(saved) == 1 && saved[0].MessageID == "om-1" {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("approval card ids were not persisted")
}

func TestApprovePatchesCards(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusPending}
	st.cards["req-1"] = []store.ApprovalCard{{ApproverEmail: "approver@example.com", MessageID: "om-1"}}
	updater := &fakeCardUpdater{}
	h := New(Options{
		Store:   st,
		Config:  testConfig(t),
		Auth:    fakeAuth{identity: &auth.Identity{Email: "approver@example.com"}},
		Cards:   updater,
		Metrics: &metrics.Metrics{},
	})
	w := postDecision(t, h, "req-1", "approve", "looks good", nil)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("approve = %d, body=%s", w.Code, w.Body.String())
	}
	updates := updater.wait(t)
	if len(updates) != 1 {
		t.Fatalf("updates = %+v", updates)
	}
	if updates[0].outcome != string(store.StatusApproved) || updates[0].actor != "approver@example.com" || updates[0].cards != 1 {
		t.Fatalf("update = %+v", updates[0])
	}
}

func TestDetailShowsNotice(t *testing.T) {
	st := newFakeStore()
	st.requests["req-1"] = &store.Request{
		ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusApproved, TTLSeconds: 3600,
	}
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com"})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, requestWithToken(http.MethodGet, "/requests/req-1?notice=already-decided", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("detail = %d", w.Code)
	}
	if !strings.Contains(w.Body.String(), "This request was already handled") {
		t.Fatalf("notice missing: %s", w.Body.String())
	}
}

func TestTTLCustomOverridesPreset(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets":    {"r.example.com/a v1 a.tar.gz"},
		"ttl":        {"2h"},
		"ttl_custom": {"90m"},
		"csrf":       {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, body=%s", w.Code, w.Body.String())
	}
	if st.created == nil || st.created.TTLSeconds != 5400 {
		t.Fatalf("ttl = %+v, want the custom 90m", st.created)
	}
}

func TestTTLPresetIsUsedWithoutCustom(t *testing.T) {
	st := newFakeStore()
	h := testHandler(t, st, &auth.Identity{Email: "requester@example.com", Subject: "sub"})
	csrf, cookie := obtainCSRF(t, h)
	form := url.Values{
		"targets": {"r.example.com/a v1 a.tar.gz"},
		"ttl":     {"8h"},
		"csrf":    {csrf},
	}
	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(form.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("create = %d, body=%s", w.Code, w.Body.String())
	}
	if st.created == nil || st.created.TTLSeconds != 28800 {
		t.Fatalf("ttl = %+v, want 8h", st.created)
	}
}
