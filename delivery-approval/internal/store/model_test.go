package store

import (
	"testing"
	"time"

	dbent "github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent/request"
)

func TestStatusValid(t *testing.T) {
	for _, s := range []Status{StatusPending, StatusApproved, StatusRejected, StatusRevoked, StatusExpired} {
		if !s.Valid() {
			t.Fatalf("%q should be valid", s)
		}
	}
	for _, s := range []Status{"", "unknown"} {
		if s.Valid() {
			t.Fatalf("%q should be invalid", s)
		}
	}
}

func TestClampLimit(t *testing.T) {
	for _, tt := range []struct{ in, want int }{{0, 50}, {-1, 50}, {10, 10}, {201, 200}} {
		if got := clampLimit(tt.in); got != tt.want {
			t.Fatalf("clampLimit(%d) = %d, want %d", tt.in, got, tt.want)
		}
	}
}

func TestRequestFromEnt(t *testing.T) {
	now := time.Now().UTC()
	decided := now.Add(time.Minute)
	row := &dbent.Request{
		ID:               "req-1",
		RequesterEmail:   "requester@pingcap.com",
		RequesterSub:     "sub",
		Purpose:          "customer-delivery",
		Note:             "note",
		Status:           request.StatusApproved,
		TTLSeconds:       3600,
		CreatedAt:        now,
		ExpiresAt:        now.Add(time.Hour),
		DecidedAt:        &decided,
		ApproverEmail:    "approver@pingcap.com",
		DecisionNote:     "ok",
		DeliveryStatus:   request.DeliveryStatusDelivered,
		DeliveryAttempts: 1,
		LastErrorClass:   "",
		Edges: dbent.RequestEdges{
			Files: []*dbent.RequestFile{{
				ID:                    "file-1",
				RequestID:             "req-1",
				Repository:            "us-docker.pkg.dev/pingcap-testing-account/hub/tidb",
				Tag:                   "v8.1.0_linux_amd64",
				Filename:              "tidb.tar.gz",
				ShareID:               "share-1",
				DownloadURLCiphertext: []byte("cipher"),
				DeliveredAt:           &decided,
				CreatedAt:             now,
			}},
			Events: []*dbent.RequestEvent{{ID: 1, RequestID: "req-1", Action: "created", Outcome: "ok", CreatedAt: now}},
		},
	}
	got := requestFromEnt(row)
	if got.ID != "req-1" || got.Status != StatusApproved || got.DeliveryStatus != DeliveryDelivered {
		t.Fatalf("request = %+v", got)
	}
	if got.DecidedAt == nil || !got.DecidedAt.Equal(decided) || got.ApproverEmail != "approver@pingcap.com" {
		t.Fatalf("decision = %+v", got)
	}
	if len(got.Files) != 1 || got.Files[0].ShareID != "share-1" || string(got.Files[0].DownloadURLCiphertext) != "cipher" {
		t.Fatalf("files = %+v", got.Files)
	}
	if len(row.Edges.Events) != 1 || row.Edges.Events[0].Action != "created" {
		t.Fatalf("events = %+v", row.Edges.Events)
	}
}
