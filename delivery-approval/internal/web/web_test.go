package web

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
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
	requests     map[string]*store.Request
	pending      []*store.Request
	pendingCount int
	recentCount  int
	created      *store.Request
}

func newFakeStore() *fakeStore {
	return &fakeStore{requests: map[string]*store.Request{}}
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
func (f *fakeStore) ListPending(context.Context, int) ([]*store.Request, error) {
	return f.pending, nil
}
func (f *fakeStore) Transition(context.Context, string, store.Status, store.Status, string, string, time.Time) (bool, error) {
	return false, nil
}
func (f *fakeStore) SetDelivery(context.Context, string, store.DeliveryStatus, int, string) error {
	return nil
}
func (f *fakeStore) AppendEvent(context.Context, *store.Event) error          { return nil }
func (f *fakeStore) SaveFileCiphertext(context.Context, string, []byte) error { return nil }
func (f *fakeStore) ClearFileCiphertext(context.Context, string) error        { return nil }
func (f *fakeStore) Close() error                                             { return nil }

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
	m := csrfPattern.FindStringSubmatch(w.Body.String())
	if len(m) != 2 || m[1] == "" {
		t.Fatalf("CSRF token missing from form: %s", w.Body.String())
	}
	if len(w.Result().Cookies()) == 0 {
		t.Fatal("CSRF cookie not set")
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
		"repository": {"us-docker.pkg.dev/project/hub/package"},
		"tag":        {"v8.1.0_linux_amd64"},
		"files":      {"tidb.tar.gz\ntikv.tar.gz"},
		"ttl":        {"3600"},
		"note":       {"customer asked"},
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
	valid := url.Values{"repository": {"r.example.com/a"}, "tag": {"v1"}, "files": {"a.tar.gz"}, "ttl": {"60"}}

	r := requestWithToken(http.MethodPost, "/requests", strings.NewReader(valid.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("missing CSRF = %d, want 403", w.Code)
	}

	csrf, cookie := obtainCSRF(t, h)
	bad := url.Values{"repository": {"../evil"}, "tag": {"v1"}, "files": {"a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf}}
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
	form := url.Values{"repository": {"r.example.com/a"}, "tag": {"v1"}, "files": {"a.tar.gz"}, "ttl": {"60"}, "csrf": {csrf}}
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
		{"filename", url.Values{"repository": {"r.example.com/a"}, "tag": {"v1"}, "files": {"../secret"}, "ttl": {"60"}}, "invalid filename"},
		{"ttl", url.Values{"repository": {"r.example.com/a"}, "tag": {"v1"}, "files": {"a.tar.gz"}, "ttl": {"0"}}, "TTL must be"},
		{"too many", url.Values{"repository": {"r.example.com/a"}, "tag": {"v1"}, "files": {"a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk"}, "ttl": {"60"}}, "provide 1-10 files"},
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
