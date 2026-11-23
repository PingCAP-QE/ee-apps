// Package approval owns scheduled approval lifecycle work.
package approval

import (
	"context"
	"log"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

// Revoker withdraws already-issued dl shares. Phase 4 supplies the HTTP client;
// nil is valid while the share delivery path is not wired yet.
type Revoker interface {
	RevokeShare(ctx context.Context, shareID string) error
}

type Sweeper struct {
	Store    store.Store
	Revoker  Revoker
	Logger   *log.Logger
	Interval time.Duration
	Batch    int
}

func NewSweeper(st store.Store, revoker Revoker, logger *log.Logger) *Sweeper {
	if logger == nil {
		logger = log.Default()
	}
	return &Sweeper{Store: st, Revoker: revoker, Logger: logger, Interval: time.Minute, Batch: 100}
}

// Run performs an initial sweep and then repeats until ctx is cancelled.
func (s *Sweeper) Run(ctx context.Context) {
	s.sweep(ctx)
	ticker := time.NewTicker(s.interval())
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.sweep(ctx)
		}
	}
}

func (s *Sweeper) interval() time.Duration {
	if s.Interval <= 0 {
		return time.Minute
	}
	return s.Interval
}

func (s *Sweeper) batch() int {
	if s.Batch <= 0 {
		return 100
	}
	return s.Batch
}

func (s *Sweeper) sweep(ctx context.Context) {
	now := time.Now().UTC()
	expired, err := s.Store.ListExpired(ctx, now, s.batch())
	if err != nil {
		s.Logger.Print("approval expiry sweep failed")
		return
	}
	for _, req := range expired {
		if req.Status == store.StatusApproved && s.Revoker != nil {
			for _, f := range req.Files {
				if f.ShareID == "" {
					continue
				}
				if err := s.Revoker.RevokeShare(ctx, f.ShareID); err != nil {
					s.Logger.Printf("approval share revoke failed request=%s", req.ID)
				}
			}
		}
		ok, err := s.Store.Transition(ctx, req.ID, req.Status, store.StatusExpired, "", "", now)
		if err != nil {
			s.Logger.Printf("approval expiry transition failed request=%s", req.ID)
			continue
		}
		if !ok {
			continue
		}
	}
}
