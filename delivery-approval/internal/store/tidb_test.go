package store

import (
	"context"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent/requestevent"
)

func testTiDB(t *testing.T) *TiDB {
	t.Helper()
	dsn := os.Getenv("DL_APPROVAL_TEST_TIDB_DSN")
	if dsn == "" {
		t.Skip("set DL_APPROVAL_TEST_TIDB_DSN to a disposable TiDB database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	st, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	if err := st.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	// The CI database is disposable; keep each test independent.
	if _, err := st.client.RequestEvent.Delete().Exec(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := st.client.RequestFile.Delete().Exec(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := st.client.Request.Delete().Exec(ctx); err != nil {
		t.Fatal(err)
	}
	return st
}

func sampleRequest(requester string) *Request {
	now := time.Now().UTC().Truncate(time.Microsecond)
	return &Request{
		RequesterEmail: requester,
		RequesterSub:   "sub-" + requester,
		Purpose:        "customer-delivery",
		Note:           "customer asked for a build",
		Status:         StatusPending,
		TTLSeconds:     3600,
		CreatedAt:      now,
		ExpiresAt:      now.Add(time.Hour),
		Files: []File{
			{Repository: "us-docker.pkg.dev/pingcap-testing-account/hub/tidb", Tag: "v8.1.0_linux_amd64", Filename: "tidb.tar.gz"},
			{Repository: "us-docker.pkg.dev/pingcap-testing-account/hub/tidb", Tag: "v8.1.0_linux_amd64", Filename: "tikv.tar.gz"},
		},
	}
}

func TestCreateGetAndList(t *testing.T) {
	st := testTiDB(t)
	ctx := context.Background()
	req := sampleRequest("requester@example.com")
	if err := st.CreateRequest(ctx, req); err != nil {
		t.Fatal(err)
	}
	if req.ID == "" || len(req.Files) != 2 || req.Files[0].ID == "" {
		t.Fatalf("ids not assigned: %+v", req)
	}

	got, err := st.GetRequest(ctx, req.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != StatusPending || got.DeliveryStatus != DeliveryPending || len(got.Files) != 2 {
		t.Fatalf("unexpected request: %+v", got)
	}
	if got.Files[0].Filename != "tidb.tar.gz" || got.Files[1].Filename != "tikv.tar.gz" {
		t.Fatalf("unexpected files: %+v", got.Files)
	}

	if n, err := st.CountPendingByRequester(ctx, "requester@example.com"); err != nil || n != 1 {
		t.Fatalf("count pending = %d, err = %v", n, err)
	}
	if n, err := st.CountRequestsSince(ctx, "requester@example.com", time.Now().UTC().Add(-time.Hour)); err != nil || n != 1 {
		t.Fatalf("count recent = %d, err = %v", n, err)
	}
	pending, err := st.ListPending(ctx, 10)
	if err != nil || len(pending) != 1 {
		t.Fatalf("pending = %d, err = %v", len(pending), err)
	}
	mine, err := st.ListByRequester(ctx, "requester@example.com", 10)
	if err != nil || len(mine) != 1 {
		t.Fatalf("by requester = %d, err = %v", len(mine), err)
	}
	other, err := st.ListByRequester(ctx, "other@example.com", 10)
	if err != nil || len(other) != 0 {
		t.Fatalf("other requester = %d, err = %v", len(other), err)
	}
	if _, err := st.GetRequest(ctx, "missing"); err != ErrNotFound {
		t.Fatalf("missing error = %v", err)
	}
}

func TestTransitionIsConditional(t *testing.T) {
	st := testTiDB(t)
	ctx := context.Background()
	req := sampleRequest("approver-test@example.com")
	if err := st.CreateRequest(ctx, req); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	results := make(chan bool, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ok, err := st.Transition(ctx, req.ID, StatusPending, StatusApproved, "approver@example.com", "ok", time.Now().UTC())
			if err != nil {
				t.Errorf("transition: %v", err)
				return
			}
			results <- ok
		}()
	}
	wg.Wait()
	close(results)
	success := 0
	for ok := range results {
		if ok {
			success++
		}
	}
	if success != 1 {
		t.Fatalf("successful transitions = %d, want 1", success)
	}

	got, err := st.GetRequest(ctx, req.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != StatusApproved || got.ApproverEmail != "approver@example.com" || got.DecidedAt == nil {
		t.Fatalf("unexpected approved request: %+v", got)
	}
	if ok, err := st.Transition(ctx, req.ID, StatusPending, StatusRejected, "approver@example.com", "", time.Now().UTC()); err != nil || ok {
		t.Fatalf("second transition = %v, %v; want false, nil", ok, err)
	}
}

func TestCiphertextAndDelivery(t *testing.T) {
	st := testTiDB(t)
	ctx := context.Background()
	req := sampleRequest("delivery-test@example.com")
	if err := st.CreateRequest(ctx, req); err != nil {
		t.Fatal(err)
	}
	fileID := req.Files[0].ID
	if err := st.SaveFileCiphertext(ctx, fileID, []byte("ciphertext")); err != nil {
		t.Fatal(err)
	}
	got, err := st.GetRequest(ctx, req.ID)
	if err != nil {
		t.Fatal(err)
	}
	if string(got.Files[0].DownloadURLCiphertext) != "ciphertext" {
		t.Fatalf("ciphertext = %q", got.Files[0].DownloadURLCiphertext)
	}
	if err := st.ClearFileCiphertext(ctx, fileID); err != nil {
		t.Fatal(err)
	}
	got, err = st.GetRequest(ctx, req.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Files[0].DownloadURLCiphertext) != 0 {
		t.Fatalf("ciphertext not cleared: %q", got.Files[0].DownloadURLCiphertext)
	}

	if err := st.SetDelivery(ctx, req.ID, DeliveryDelivered, 1, ""); err != nil {
		t.Fatal(err)
	}
	got, err = st.GetRequest(ctx, req.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.DeliveryStatus != DeliveryDelivered || got.DeliveryAttempts != 1 {
		t.Fatalf("delivery = %+v", got)
	}

	if err := st.AppendEvent(ctx, &Event{RequestID: req.ID, FileID: fileID, ActorEmail: "approver@example.com", Action: "delivered", Outcome: "ok"}); err != nil {
		t.Fatal(err)
	}
	count, err := st.client.RequestEvent.Query().Count(ctx)
	if err != nil || count != 2 {
		t.Fatalf("events = %d, err = %v", count, err)
	}
}

func TestMigrateIsIdempotent(t *testing.T) {
	st := testTiDB(t)
	ctx := context.Background()
	if err := st.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if err := st.Ready(ctx); err != nil {
		t.Fatal(err)
	}
}

func expiredRequest(requester string, status Status) *Request {
	req := sampleRequest(requester)
	req.Status = status
	req.CreatedAt = time.Now().UTC().Add(-2 * time.Hour).Truncate(time.Microsecond)
	req.ExpiresAt = time.Now().UTC().Add(-time.Hour).Truncate(time.Microsecond)
	req.TTLSeconds = 3600
	return req
}

func TestListExpiredAndTransitionEvent(t *testing.T) {
	st := testTiDB(t)
	ctx := context.Background()
	pending := expiredRequest("pending@example.com", StatusPending)
	approved := expiredRequest("approved@example.com", StatusApproved)
	rejected := expiredRequest("rejected@example.com", StatusRejected)
	for _, req := range []*Request{pending, approved, rejected} {
		if err := st.CreateRequest(ctx, req); err != nil {
			t.Fatal(err)
		}
	}
	expired, err := st.ListExpired(ctx, time.Now().UTC(), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(expired) != 2 {
		t.Fatalf("expired = %d, want 2", len(expired))
	}
	ok, err := st.Transition(ctx, pending.ID, StatusPending, StatusExpired, "", "", time.Now().UTC())
	if err != nil || !ok {
		t.Fatalf("expire pending = %v, %v", ok, err)
	}
	ok, err = st.Transition(ctx, pending.ID, StatusPending, StatusExpired, "", "", time.Now().UTC())
	if err != nil || ok {
		t.Fatalf("second expire = %v, %v; want false, nil", ok, err)
	}
	count, err := st.client.RequestEvent.Query().
		Where(requestevent.RequestIDEQ(pending.ID), requestevent.ActionEQ("expired")).
		Count(ctx)
	if err != nil || count != 1 {
		t.Fatalf("expired events = %d, err = %v", count, err)
	}
}
