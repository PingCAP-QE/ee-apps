package impl

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"
)

const (
	// nightlyFailureAlertInterval bounds how often the aggregated failure alert
	// is sent. A refusing mirror fails every channel of the window within a few
	// minutes; one message per failed unit would be noise, while one batch per
	// interval carrying the channel count and the first error is what an on-call
	// needs to tell "one bad package" from "the mirror rejects everything".
	nightlyFailureAlertInterval = 10 * time.Minute
	// maxNightlyFailureSamples bounds the channel list in the alert body.
	maxNightlyFailureSamples = 8
	// maxNightlyFailureErrorLen bounds the error text in the alert body.
	maxNightlyFailureErrorLen = 300
)

// nightlyFailureAccumulator batches the nightly units that settled as a failure
// between two ticks. Settles happen asynchronously in the worker, so a batch is
// the natural unit of the alert.
type nightlyFailureAccumulator struct {
	mu       sync.Mutex
	rows     []string
	versions []string
	errors   []string
	lastSent time.Time
}

func newNightlyFailureAccumulator() *nightlyFailureAccumulator {
	return &nightlyFailureAccumulator{}
}

func (a *nightlyFailureAccumulator) add(row, version, detail string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.rows = append(a.rows, row)
	if version != "" {
		a.versions = append(a.versions, version)
	}
	if detail != "" {
		a.errors = append(a.errors, detail)
	}
}

// take returns the pending batch and clears it.
func (a *nightlyFailureAccumulator) take() (rows, versions, details []string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	rows, versions, details = a.rows, a.versions, a.errors
	a.rows, a.versions, a.errors = nil, nil, nil
	return rows, versions, details
}

// allow reports whether an alert may be sent now, and records the attempt.
func (a *nightlyFailureAccumulator) allow(now time.Time) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.lastSent.IsZero() && now.Sub(a.lastSent) < nightlyFailureAlertInterval {
		return false
	}
	a.lastSent = now
	return true
}

// flushNightlyFailureAlert sends one aggregated alert for the nightly rows whose
// publication failed since the previous tick. A failure keeps the row retrying in
// the next window, so without this alert the only signal is the lag alert, which
// fires 1.5 windows later and compares version strings — constant for a nightly
// channel, so it cannot tell a stalled publication from a delivered one.
func (s *publishsrvc) flushNightlyFailureAlert(ctx context.Context) {
	if s.failedNightly == nil || s.alerter == nil {
		return
	}
	rows, versions, details := s.failedNightly.take()
	if len(rows) == 0 {
		return
	}
	if !s.failedNightly.allow(s.now()) {
		s.logger.Info().
			Int("channels", len(rows)).
			Msg("nightly failure alert skipped: the previous one is still within the interval")
		return
	}

	body := fmt.Sprintf("channels=%d", len(rows))
	if len(versions) > 0 {
		body += " version=" + versions[0]
	}
	body += " sample=" + strings.Join(nightlyFailureSamples(rows), ",")
	if len(details) > 0 {
		body += " first_error=" + truncateForAlert(details[0])
	}
	if err := s.alerter.SendAlert(ctx, "[tibuild] nightly publish failed", body); err != nil {
		s.logger.Err(err).Int("channels", len(rows)).Msg("failed to send the nightly failure alert")
	}
}

// nightlyFailureSamples keeps the alert body readable when a whole window fails.
func nightlyFailureSamples(rows []string) []string {
	if len(rows) <= maxNightlyFailureSamples {
		return rows
	}
	more := len(rows) - maxNightlyFailureSamples
	return append(rows[:maxNightlyFailureSamples:maxNightlyFailureSamples], fmt.Sprintf("+%d more", more))
}

func truncateForAlert(message string) string {
	if len(message) <= maxNightlyFailureErrorLen {
		return message
	}
	return message[:maxNightlyFailureErrorLen] + "…"
}
