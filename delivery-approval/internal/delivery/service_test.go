package delivery

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/dl"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/secure"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type fakeStore struct {
	req      *store.Request
	delivery store.DeliveryStatus
	attempts int
	class    string
	events   []store.Event
	shareErr error
}

func (f *fakeStore) Ready(context.Context) error                         { return nil }
func (f *fakeStore) CreateRequest(context.Context, *store.Request) error { return nil }
func (f *fakeStore) GetRequest(context.Context, string) (*store.Request, error) {
	if f.req == nil {
		return nil, store.ErrNotFound
	}
	return f.req, nil
}
func (f *fakeStore) ListByRequester(context.Context, string, int) ([]*store.Request, error) {
	return nil, nil
}
func (f *fakeStore) CountPendingByRequester(context.Context, string) (int, error) { return 0, nil }
func (f *fakeStore) CountRequestsSince(context.Context, string, time.Time) (int, error) {
	return 0, nil
}
func (f *fakeStore) ListExpired(context.Context, time.Time, int) ([]*store.Request, error) {
	return nil, nil
}
func (f *fakeStore) ListPending(context.Context, int) ([]*store.Request, error) { return nil, nil }
func (f *fakeStore) Transition(context.Context, string, store.Status, store.Status, string, string, time.Time) (bool, error) {
	return false, nil
}
func (f *fakeStore) SetDelivery(_ context.Context, _ string, status store.DeliveryStatus, attempts int, class string) error {
	f.delivery, f.attempts, f.class = status, attempts, class
	return nil
}
func (f *fakeStore) AppendEvent(_ context.Context, event *store.Event) error {
	f.events = append(f.events, *event)
	return nil
}
func (f *fakeStore) SetFileShare(_ context.Context, fileID, shareID string, ciphertext []byte) error {
	if f.shareErr != nil {
		return f.shareErr
	}
	for i := range f.req.Files {
		if f.req.Files[i].ID == fileID {
			f.req.Files[i].ShareID = shareID
			f.req.Files[i].DownloadURLCiphertext = ciphertext
			return nil
		}
	}
	return errors.New("file not found")
}
func (f *fakeStore) SaveFileCiphertext(context.Context, string, []byte) error { return nil }
func (f *fakeStore) ClearFileCiphertext(_ context.Context, fileID string) error {
	for i := range f.req.Files {
		if f.req.Files[i].ID == fileID {
			f.req.Files[i].DownloadURLCiphertext = nil
		}
	}
	return nil
}
func (f *fakeStore) Close() error { return nil }

type fakeIssuer struct {
	issues  []dl.IssueRequest
	revokes []string
	failOn  string
}

func (f *fakeIssuer) Issue(_ context.Context, req dl.IssueRequest) (*dl.Share, error) {
	f.issues = append(f.issues, req)
	if req.File == f.failOn {
		return nil, errors.New("issue failed")
	}
	return &dl.Share{ID: "share-" + req.File, DownloadURL: "https://dl.example.com/d/" + req.File}, nil
}
func (f *fakeIssuer) Revoke(_ context.Context, shareID string) error {
	f.revokes = append(f.revokes, shareID)
	return nil
}

type fakeNotifier struct {
	links    []Link
	failures int
	calls    int
}

func (f *fakeNotifier) Notify(_ context.Context, _ *store.Request, links []Link) error {
	f.calls++
	if len(links) == 0 {
		return errors.New("no links")
	}
	if f.calls <= f.failures {
		return errors.New("lark unavailable")
	}
	f.links = append([]Link(nil), links...)
	return nil
}

func testSealer(t *testing.T) *secure.Sealer {
	t.Helper()
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	s, err := secure.New(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func approvedRequest() *store.Request {
	now := time.Now().UTC()
	return &store.Request{
		ID: "req-1", RequesterEmail: "requester@example.com", Status: store.StatusApproved,
		CreatedAt: now, ExpiresAt: now.Add(time.Hour), DeliveryStatus: store.DeliveryPending,
		Files: []store.File{
			{ID: "file-1", Repository: "r.example.com/a", Tag: "v1", Filename: "a.tar.gz"},
			{ID: "file-2", Repository: "r.example.com/a", Tag: "v1", Filename: "b.tar.gz"},
		},
	}
}

func newService(t *testing.T, st *fakeStore, issuer *fakeIssuer, notifier *fakeNotifier) *Service {
	t.Helper()
	s := New(st, issuer, testSealer(t), notifier, nil)
	s.Sleep = func(time.Duration) {}
	return s
}

func TestDeliverSuccess(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	issuer := &fakeIssuer{}
	notifier := &fakeNotifier{}
	s := newService(t, st, issuer, notifier)

	if err := s.Deliver(context.Background(), st.req.ID); err != nil {
		t.Fatal(err)
	}
	if len(issuer.issues) != 2 || issuer.issues[0].ExpiresInSeconds <= 0 {
		t.Fatalf("issues = %+v", issuer.issues)
	}
	if len(notifier.links) != 2 || !strings.Contains(notifier.links[0].URL, "https://dl.example.com/d/") {
		t.Fatalf("links = %+v", notifier.links)
	}
	if st.delivery != store.DeliveryDelivered || st.attempts != 1 {
		t.Fatalf("delivery = %s attempts=%d", st.delivery, st.attempts)
	}
	for _, f := range st.req.Files {
		if f.ShareID == "" || len(f.DownloadURLCiphertext) != 0 {
			t.Fatalf("file not finalized: %+v", f)
		}
	}
	if len(st.events) != 3 || st.events[0].Action != "issued" || st.events[2].Action != "delivered" {
		t.Fatalf("events = %+v", st.events)
	}
}

func TestDeliverIssueFailureRevokesIssuedShares(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	issuer := &fakeIssuer{failOn: "b.tar.gz"}
	notifier := &fakeNotifier{}
	s := newService(t, st, issuer, notifier)

	if err := s.Deliver(context.Background(), st.req.ID); err == nil {
		t.Fatal("expected issue failure")
	}
	if len(issuer.revokes) != 1 || issuer.revokes[0] != "share-a.tar.gz" {
		t.Fatalf("revokes = %v", issuer.revokes)
	}
	if st.delivery != store.DeliveryFailed || st.class != "issue_failed" {
		t.Fatalf("delivery = %s class=%s", st.delivery, st.class)
	}
	if len(notifier.links) != 0 {
		t.Fatal("notifier called after issue failure")
	}
}

func TestDeliverNotifyRetriesThenFails(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	issuer := &fakeIssuer{}
	notifier := &fakeNotifier{failures: 99}
	s := newService(t, st, issuer, notifier)

	if err := s.Deliver(context.Background(), st.req.ID); err == nil {
		t.Fatal("expected notify failure")
	}
	if notifier.calls != 5 {
		t.Fatalf("notify calls = %d, want 5", notifier.calls)
	}
	if len(issuer.revokes) != 2 {
		t.Fatalf("revokes = %v", issuer.revokes)
	}
	if st.delivery != store.DeliveryFailed || st.class != "notify_failed" {
		t.Fatalf("delivery = %s class=%s", st.delivery, st.class)
	}
}

func TestDeliverRequiresApproved(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	st.req.Status = store.StatusPending
	s := newService(t, st, &fakeIssuer{}, &fakeNotifier{})
	if !errors.Is(s.Deliver(context.Background(), st.req.ID), ErrNotApproved) {
		t.Fatal("pending request delivered")
	}
}

func TestDeliverExistingShareSkipsIssue(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	sealer := testSealer(t)
	sealed, err := sealer.Seal("https://dl.example.com/d/existing")
	if err != nil {
		t.Fatal(err)
	}
	st.req.Files[0].ShareID = "share-existing"
	st.req.Files[0].DownloadURLCiphertext = sealed
	issuer := &fakeIssuer{}
	notifier := &fakeNotifier{}
	s := New(st, issuer, sealer, notifier, nil)
	s.Sleep = func(time.Duration) {}

	if err := s.Deliver(context.Background(), st.req.ID); err != nil {
		t.Fatal(err)
	}
	if len(issuer.issues) != 1 || issuer.issues[0].File != "b.tar.gz" {
		t.Fatalf("issues = %+v", issuer.issues)
	}
	if len(notifier.links) != 2 || notifier.links[0].URL != "https://dl.example.com/d/existing" {
		t.Fatalf("links = %+v", notifier.links)
	}
}

func TestDeliverMissingSealedURL(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	st.req.Files[0].ShareID = "share-existing"
	s := newService(t, st, &fakeIssuer{}, &fakeNotifier{})
	if !errors.Is(s.Deliver(context.Background(), st.req.ID), ErrSealedURL) {
		t.Fatal("missing ciphertext accepted")
	}
	if st.delivery != store.DeliveryFailed || st.class != "sealed_url_missing" {
		t.Fatalf("delivery = %s class=%s", st.delivery, st.class)
	}
}

func TestDeliverExpired(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	st.req.ExpiresAt = time.Now().UTC().Add(-time.Minute)
	s := newService(t, st, &fakeIssuer{}, &fakeNotifier{})
	if !errors.Is(s.Deliver(context.Background(), st.req.ID), ErrExpired) {
		t.Fatal("expired request delivered")
	}
	if st.delivery != store.DeliveryFailed || st.class != "expired" {
		t.Fatalf("delivery = %s class=%s", st.delivery, st.class)
	}
}

func TestDeliverWithoutNotifier(t *testing.T) {
	st := &fakeStore{req: approvedRequest()}
	s := New(st, &fakeIssuer{}, testSealer(t), nil, nil)
	s.Sleep = func(time.Duration) {}
	if !errors.Is(s.Deliver(context.Background(), st.req.ID), ErrNotifierUnavailable) {
		t.Fatal("missing notifier accepted")
	}
	if st.delivery != store.DeliveryFailed || st.class != "notifier_unavailable" {
		t.Fatalf("delivery = %s class=%s", st.delivery, st.class)
	}
}

func TestDeliverShareStoreError(t *testing.T) {
	st := &fakeStore{req: approvedRequest(), shareErr: errors.New("db down")}
	issuer := &fakeIssuer{}
	s := newService(t, st, issuer, &fakeNotifier{})
	if err := s.Deliver(context.Background(), st.req.ID); err == nil {
		t.Fatal("store error ignored")
	}
	if len(issuer.revokes) != 1 || issuer.revokes[0] != "share-a.tar.gz" {
		t.Fatalf("revokes = %v", issuer.revokes)
	}
	if st.class != "store_failed" {
		t.Fatalf("class = %s", st.class)
	}
}
