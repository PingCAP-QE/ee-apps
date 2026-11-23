package server

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type fakeStore struct {
	readyErr error
}

func (f *fakeStore) Ready(context.Context) error                         { return f.readyErr }
func (f *fakeStore) CreateRequest(context.Context, *store.Request) error { return nil }
func (f *fakeStore) GetRequest(context.Context, string) (*store.Request, error) {
	return nil, store.ErrNotFound
}
func (f *fakeStore) ListByRequester(context.Context, string, int) ([]*store.Request, error) {
	return nil, nil
}
func (f *fakeStore) CountPendingByRequester(context.Context, string) (int, error) {
	return 0, nil
}
func (f *fakeStore) CountRequestsSince(context.Context, string, time.Time) (int, error) {
	return 0, nil
}
func (f *fakeStore) ListPending(context.Context, int) ([]*store.Request, error) { return nil, nil }
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

func TestProbes(t *testing.T) {
	m := &metrics.Metrics{}
	m.Created.Add(2)
	h := Probes(Options{Store: &fakeStore{}, Metrics: m})

	for _, path := range []string{"/livez", "/healthz"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d, want 200", path, rec.Code)
		}
	}

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "dl_approval_requests_created_total 2") {
		t.Fatalf("metrics response = %d %q", rec.Code, rec.Body.String())
	}
}

func TestHealthFailsWhenStoreUnavailable(t *testing.T) {
	h := Probes(Options{Store: &fakeStore{readyErr: errors.New("db down")}})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("health = %d, want 503", rec.Code)
	}

	rec = httptest.NewRecorder()
	Probes(Options{}).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil store health = %d, want 503", rec.Code)
	}
}
