// Package delivery turns an approved request into dl shares and delivers the
// one-time URLs through a Notifier. Plaintext URLs exist only in memory and in
// the sealed column until delivery succeeds.
package delivery

import (
	"context"
	"errors"
	"log"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/dl"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/secure"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

var (
	ErrNotApproved         = errors.New("request is not approved")
	ErrNotifierUnavailable = errors.New("delivery notifier is not configured")
	ErrExpired             = errors.New("request expired before delivery")
	ErrSealedURL           = errors.New("sealed URL is invalid")
)

type Issuer interface {
	Issue(ctx context.Context, req dl.IssueRequest) (*dl.Share, error)
	Revoke(ctx context.Context, shareID string) error
}

type Notifier interface {
	Notify(ctx context.Context, req *store.Request, links []Link) error
}

type Link struct {
	File store.File
	URL  string
}

type Service struct {
	Store    store.Store
	Issuer   Issuer
	Sealer   *secure.Sealer
	Notifier Notifier
	Logger   *log.Logger

	NotifyAttempts int
	NotifyBackoff  time.Duration
	Sleep          func(time.Duration)
}

func New(st store.Store, issuer Issuer, sealer *secure.Sealer, notifier Notifier, logger *log.Logger) *Service {
	if logger == nil {
		logger = log.Default()
	}
	return &Service{
		Store: st, Issuer: issuer, Sealer: sealer, Notifier: notifier, Logger: logger,
		NotifyAttempts: 5,
		NotifyBackoff:  500 * time.Millisecond,
		Sleep:          time.Sleep,
	}
}

// Deliver issues one share per file, seals each URL at rest, and notifies the
// requester. Any failure revokes every issued share and records delivery_failed.
func (s *Service) Deliver(ctx context.Context, requestID string) error {
	if s.Issuer == nil || s.Sealer == nil {
		return errors.New("delivery is not configured")
	}
	req, err := s.Store.GetRequest(ctx, requestID)
	if err != nil {
		return err
	}
	if req.Status != store.StatusApproved {
		return ErrNotApproved
	}
	if req.DeliveryStatus == store.DeliveryDelivered {
		return nil
	}
	if req.DeliveryStatus == store.DeliveryDelivering {
		// A previous process died mid-delivery. Retry from the stored share IDs
		// and sealed URLs; do not create a second share for a file that has one.
	}
	attempts := req.DeliveryAttempts + 1
	if err := s.Store.SetDelivery(ctx, req.ID, store.DeliveryDelivering, attempts, ""); err != nil {
		return err
	}

	for i := range req.Files {
		f := &req.Files[i]
		if f.ShareID != "" {
			if len(f.DownloadURLCiphertext) == 0 {
				s.abort(ctx, req, attempts, "sealed_url_missing", ErrSealedURL)
				return ErrSealedURL
			}
			continue
		}
		ttl := int64(time.Until(req.ExpiresAt).Seconds())
		if ttl < 1 {
			s.abort(ctx, req, attempts, "expired", ErrExpired)
			return ErrExpired
		}
		share, err := s.Issuer.Issue(ctx, dl.IssueRequest{
			Repository:       f.Repository,
			Tag:              f.Tag,
			File:             f.Filename,
			ExpiresInSeconds: ttl,
			Purpose:          "customer-delivery",
		})
		if err != nil {
			s.abort(ctx, req, attempts, "issue_failed", err)
			return err
		}
		// Record the share on the in-memory request before any further step so
		// abort() can revoke it if sealing or persistence fails.
		f.ShareID = share.ID
		sealed, err := s.Sealer.Seal(share.DownloadURL)
		if err != nil {
			s.abort(ctx, req, attempts, "seal_failed", err)
			return err
		}
		f.DownloadURLCiphertext = sealed
		if err := s.Store.SetFileShare(ctx, f.ID, share.ID, sealed); err != nil {
			s.abort(ctx, req, attempts, "store_failed", err)
			return err
		}
		s.event(ctx, req.ID, f.ID, "issued", "ok", "")
	}

	links, err := s.links(req)
	if err != nil {
		s.abort(ctx, req, attempts, "sealed_url_invalid", err)
		return err
	}
	if s.Notifier == nil {
		s.abort(ctx, req, attempts, "notifier_unavailable", ErrNotifierUnavailable)
		return ErrNotifierUnavailable
	}
	if err := s.notify(ctx, req, links); err != nil {
		s.abort(ctx, req, attempts, "notify_failed", err)
		return err
	}

	for _, f := range req.Files {
		if err := s.Store.ClearFileCiphertext(ctx, f.ID); err != nil {
			s.Logger.Printf("delivery ciphertext cleanup failed request=%s", req.ID)
		}
	}
	if err := s.Store.SetDelivery(ctx, req.ID, store.DeliveryDelivered, attempts, ""); err != nil {
		return err
	}
	s.event(ctx, req.ID, "", "delivered", "ok", "")
	return nil
}

func (s *Service) links(req *store.Request) ([]Link, error) {
	links := make([]Link, 0, len(req.Files))
	for _, f := range req.Files {
		url, err := s.Sealer.Open(f.DownloadURLCiphertext)
		if err != nil {
			return nil, ErrSealedURL
		}
		links = append(links, Link{File: f, URL: url})
	}
	return links, nil
}

func (s *Service) notify(ctx context.Context, req *store.Request, links []Link) error {
	attempts := s.NotifyAttempts
	if attempts < 1 {
		// Clamp instead of looping forever; a zero value means one attempt.
		attempts = 1
	}
	backoff := s.NotifyBackoff
	if backoff < 0 {
		backoff = 0
	}
	var last error
	for i := 0; i < attempts; i++ {
		if i > 0 {
			s.Sleep(time.Duration(i) * backoff)
		}
		if err := s.Notifier.Notify(ctx, req, links); err == nil {
			return nil
		} else {
			last = err
		}
	}
	if last == nil {
		last = errors.New("delivery notification failed")
	}
	return last
}

// abort revokes every share best-effort, clears sealed URLs, and records the
// failure. A failed revoke leaves the share to its own TTL; the token is never
// logged.
func (s *Service) abort(ctx context.Context, req *store.Request, attempts int, class string, cause error) {
	for _, f := range req.Files {
		if f.ShareID != "" && s.Issuer != nil {
			if err := s.Issuer.Revoke(ctx, f.ShareID); err != nil {
				s.Logger.Printf("delivery share revoke failed request=%s share=%s", req.ID, f.ShareID)
			}
		}
		_ = s.Store.ClearFileCiphertext(ctx, f.ID)
	}
	if err := s.Store.SetDelivery(ctx, req.ID, store.DeliveryFailed, attempts, class); err != nil {
		s.Logger.Printf("delivery status update failed request=%s", req.ID)
	}
	s.event(ctx, req.ID, "", "delivery_failed", "error", class)
}

func (s *Service) event(ctx context.Context, requestID, fileID, action, outcome, class string) {
	if err := s.Store.AppendEvent(ctx, &store.Event{
		RequestID: requestID, FileID: fileID, Action: action, Outcome: outcome, ErrorClass: class,
	}); err != nil {
		s.Logger.Printf("delivery audit write failed request=%s", requestID)
	}
}
