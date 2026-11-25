package approval

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type fakeStore struct {
	expired       []*store.Request
	transitions   []string
	listErr       error
	transitionErr error
}

func (f *fakeStore) Ready(context.Context) error                         { return nil }
func (f *fakeStore) CreateRequest(context.Context, *store.Request) error { return nil }
func (f *fakeStore) GetRequest(context.Context, string) (*store.Request, error) {
	return nil, store.ErrNotFound
}
func (f *fakeStore) ListByRequester(context.Context, string, int) ([]*store.Request, error) {
	return nil, nil
}
func (f *fakeStore) CountPendingByRequester(context.Context, string) (int, error) { return 0, nil }
func (f *fakeStore) CountRequestsSince(context.Context, string, time.Time) (int, error) {
	return 0, nil
}
func (f *fakeStore) ListExpired(context.Context, time.Time, int) ([]*store.Request, error) {
	return f.expired, f.listErr
}
func (f *fakeStore) ListPending(context.Context, int) ([]*store.Request, error) { return nil, nil }
func (f *fakeStore) Transition(_ context.Context, id string, from, to store.Status, _, _ string, _ time.Time) (bool, error) {
	if from != store.StatusPending && from != store.StatusApproved {
		return false, nil
	}
	f.transitions = append(f.transitions, id)
	return true, f.transitionErr
}
func (f *fakeStore) SetDelivery(context.Context, string, store.DeliveryStatus, int, string) error {
	return nil
}
func (f *fakeStore) AppendEvent(context.Context, *store.Event) error            { return nil }
func (f *fakeStore) SetFileShare(context.Context, string, string, []byte) error { return nil }
func (f *fakeStore) SaveFileCiphertext(context.Context, string, []byte) error   { return nil }
func (f *fakeStore) ClearFileCiphertext(context.Context, string) error          { return nil }
func (f *fakeStore) SaveApprovalCards(context.Context, string, []store.ApprovalCard) error {
	return nil
}

func (f *fakeStore) ListApprovalCards(context.Context, string) ([]store.ApprovalCard, error) {
	return nil, nil
}

func (f *fakeStore) Close() error { return nil }

type fakeRevoker struct {
	shares []string
	err    error
}

func (f *fakeRevoker) RevokeShare(_ context.Context, shareID string) error {
	f.shares = append(f.shares, shareID)
	return f.err
}

func TestSweepExpiresOnlyPendingAndApproved(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{
		{ID: "pending", Status: store.StatusPending},
		{ID: "approved", Status: store.StatusApproved, Files: []store.File{{ShareID: "share-1"}, {ShareID: ""}}},
		{ID: "rejected", Status: store.StatusRejected},
		{ID: "revoked", Status: store.StatusRevoked},
	}}
	revoker := &fakeRevoker{}
	s := NewSweeper(st, revoker, nil)
	s.sweep(context.Background())

	if len(st.transitions) != 2 || st.transitions[0] != "pending" || st.transitions[1] != "approved" {
		t.Fatalf("transitions = %v", st.transitions)
	}
	if len(revoker.shares) != 1 || revoker.shares[0] != "share-1" {
		t.Fatalf("revoked shares = %v", revoker.shares)
	}
}

func TestSweepContinuesWhenRevokeFails(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{{
		ID: "approved", Status: store.StatusApproved,
		Files: []store.File{{ShareID: "share-1"}},
	}}}
	s := NewSweeper(st, &fakeRevoker{err: errors.New("revoke failed")}, nil)
	s.sweep(context.Background())
	if len(st.transitions) != 1 {
		t.Fatalf("transition did not happen after revoke failure: %v", st.transitions)
	}
}

func TestSweepListErrorIsBestEffort(t *testing.T) {
	st := &fakeStore{listErr: errors.New("db down")}
	NewSweeper(st, nil, nil).sweep(context.Background())
	if len(st.transitions) != 0 {
		t.Fatalf("unexpected transitions: %v", st.transitions)
	}
}

func TestSweepTransitionErrorDoesNotPanic(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{{ID: "pending", Status: store.StatusPending}}, transitionErr: errors.New("db down")}
	NewSweeper(st, nil, nil).sweep(context.Background())
	if len(st.transitions) != 1 {
		t.Fatalf("transitions = %v", st.transitions)
	}
}

func TestRunSweepsOnceAndStops(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{{ID: "pending", Status: store.StatusPending}}}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	NewSweeper(st, nil, nil).Run(ctx)
	if len(st.transitions) != 1 {
		t.Fatalf("transitions = %v", st.transitions)
	}
}

type fakeNotifier struct {
	requests []string
	hadLinks []bool
	err      error
}

func (f *fakeNotifier) NotifyExpired(_ context.Context, req *store.Request, hadLinks bool) error {
	f.requests = append(f.requests, req.ID)
	f.hadLinks = append(f.hadLinks, hadLinks)
	return f.err
}

func TestSweepNotifiesTheRequester(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{
		{ID: "pending-one", RequesterEmail: "requester@example.com", Status: store.StatusPending},
		{ID: "approved-one", RequesterEmail: "requester@example.com", Status: store.StatusApproved},
	}}
	notifier := &fakeNotifier{}
	s := NewSweeper(st, nil, nil)
	s.Notifier = notifier
	s.sweep(context.Background())

	if len(notifier.requests) != 2 {
		t.Fatalf("notified %v, want both requests", notifier.requests)
	}
	// List order: a pending request never had links, an approved one did.
	if notifier.hadLinks[0] || !notifier.hadLinks[1] {
		t.Fatalf("hadLinks = %v, want [false true] in list order", notifier.hadLinks)
	}
}

func TestSweepSurvivesNotificationFailure(t *testing.T) {
	st := &fakeStore{expired: []*store.Request{
		{ID: "one", Status: store.StatusApproved},
	}}
	s := NewSweeper(st, nil, nil)
	s.Notifier = &fakeNotifier{err: errors.New("lark down")}
	s.sweep(context.Background())

	if len(st.transitions) != 1 {
		t.Fatalf("transitions = %v, want the request expired anyway", st.transitions)
	}
}
