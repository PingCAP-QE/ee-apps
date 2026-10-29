// Package worker contains the reusable polling loop used by the in-process
// tibuild worker and the standalone tibuild-worker binary.
package worker

import (
	"context"
	"time"

	"github.com/rs/zerolog"
)

// DefaultPollInterval is used when Runner.PollInterval is not positive.
const DefaultPollInterval = 10 * time.Second

// ConsumeFunc performs one claim-and-process iteration. Implementations must
// return promptly when the context is cancelled.
type ConsumeFunc func(ctx context.Context)

// Runner drives a ConsumeFunc on a fixed interval until the context is
// cancelled. It does not run the consumer immediately; the first call happens
// after one PollInterval, matching the legacy image-sync worker behavior.
type Runner struct {
	// Logger is optional.
	Logger       *zerolog.Logger
	PollInterval time.Duration
	Consume      ConsumeFunc
}

// Run blocks until ctx is cancelled.
func (r *Runner) Run(ctx context.Context) {
	interval := r.PollInterval
	if interval <= 0 {
		interval = DefaultPollInterval
	}

	if r.Consume == nil {
		r.log().Info().Dur("poll_interval", interval).Msg("no consumer configured, worker runner is idle")
		<-ctx.Done()
		return
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	r.log().Info().Dur("poll_interval", interval).Msg("worker runner started")
	for {
		select {
		case <-ctx.Done():
			r.log().Info().Msg("worker runner stopped")
			return
		case <-ticker.C:
			r.Consume(ctx)
		}
	}
}

func (r *Runner) log() *zerolog.Logger {
	if r.Logger != nil {
		return r.Logger
	}
	logger := zerolog.Nop()
	return &logger
}
