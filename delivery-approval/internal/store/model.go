package store

import (
	"context"
	"errors"
	"time"
)

var ErrNotFound = errors.New("approval request not found")

type Status string

const (
	StatusPending  Status = "pending"
	StatusApproved Status = "approved"
	StatusRejected Status = "rejected"
	StatusRevoked  Status = "revoked"
	StatusExpired  Status = "expired"
)

func (s Status) Valid() bool {
	switch s {
	case StatusPending, StatusApproved, StatusRejected, StatusRevoked, StatusExpired:
		return true
	}
	return false
}

type DeliveryStatus string

const (
	DeliveryPending    DeliveryStatus = "pending"
	DeliveryDelivering DeliveryStatus = "delivering"
	DeliveryDelivered  DeliveryStatus = "delivered"
	DeliveryFailed     DeliveryStatus = "failed"
)

type Request struct {
	ID               string
	RequesterEmail   string
	RequesterSub     string
	Purpose          string
	Note             string
	Status           Status
	TTLSeconds       int64
	CreatedAt        time.Time
	ExpiresAt        time.Time
	DecidedAt        *time.Time
	ApproverEmail    string
	DecisionNote     string
	DeliveryStatus   DeliveryStatus
	DeliveryAttempts int
	LastErrorClass   string

	Files []File
}

func (r *Request) IsPending() bool { return r != nil && r.Status == StatusPending }

type File struct {
	ID                    string
	RequestID             string
	Repository            string
	Tag                   string
	Filename              string
	ShareID               string
	DownloadURLCiphertext []byte
	DeliveredAt           *time.Time
	CreatedAt             time.Time
}

type Event struct {
	RequestID  string
	FileID     string
	ActorEmail string
	Action     string
	Outcome    string
	ErrorClass string
	CreatedAt  time.Time
}

// Store is the durable approval state. Transition is conditional on the
// current status so concurrent approvers cannot both win.
type Store interface {
	Ready(ctx context.Context) error
	CreateRequest(ctx context.Context, req *Request) error
	GetRequest(ctx context.Context, id string) (*Request, error)
	ListByRequester(ctx context.Context, email string, limit int) ([]*Request, error)
	CountPendingByRequester(ctx context.Context, email string) (int, error)
	CountRequestsSince(ctx context.Context, email string, since time.Time) (int, error)
	ListExpired(ctx context.Context, before time.Time, limit int) ([]*Request, error)
	ListPending(ctx context.Context, limit int) ([]*Request, error)
	Transition(ctx context.Context, id string, from, to Status, actor, note string, decidedAt time.Time) (bool, error)
	SetDelivery(ctx context.Context, id string, status DeliveryStatus, attempts int, errorClass string) error
	AppendEvent(ctx context.Context, event *Event) error
	SaveFileCiphertext(ctx context.Context, fileID string, ciphertext []byte) error
	ClearFileCiphertext(ctx context.Context, fileID string) error
	Close() error
}
