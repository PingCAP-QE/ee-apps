package impl

import (
	"context"
	"time"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entdevbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
)

// defaultLarkRetryLookback bounds how far back the notification compensation
// scans when Lark.RetryLookback is not configured.
const defaultLarkRetryLookback = 7 * 24 * time.Hour

// parseLarkRetryLookback parses the configured lookback duration, falling back
// to defaultLarkRetryLookback when the value is empty or invalid.
func parseLarkRetryLookback(value string, logger *zerolog.Logger) time.Duration {
	if value == "" {
		return defaultLarkRetryLookback
	}
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		logger.Warn().
			Str("retry_lookback", value).
			Dur("default", defaultLarkRetryLookback).
			Msg("invalid lark retry_lookback, using default")
		return defaultLarkRetryLookback
	}
	return d
}

// hasFailedLarkNotification reports whether any Lark channel for the build is in
// a failed state, i.e. its last delivery attempt errored.
func hasFailedLarkNotification(state schema.NotificationState) bool {
	for _, entry := range state.Lark {
		if entry.Failed {
			return true
		}
	}
	return false
}

// filterFailedNotifications returns builds that have at least one failed Lark
// notification entry.
func filterFailedNotifications(builds []*ent.DevBuild) []*ent.DevBuild {
	var failed []*ent.DevBuild
	for _, build := range builds {
		if hasFailedLarkNotification(build.NotificationState) {
			failed = append(failed, build)
		}
	}
	return failed
}

// retryFailedNotifications re-sends notifications that previously failed for
// builds updated within the configured lookback window. It is intended to run
// after a successful configuration reload (for example, after fixing an invalid
// Lark app secret) so users eventually receive notifications that were lost.
//
// Re-sending is idempotent: card creation uses a deterministic UUID and existing
// cards are updated in place, so successfully delivered notifications are not
// duplicated. The persisted state has its Failed flag cleared on success, which
// stops the build from being retried again.
func (s *devbuildsrvc) retryFailedNotifications(ctx context.Context) {
	if s.larkNotifier == nil {
		return
	}

	lookback := s.larkRetryLookback
	if lookback <= 0 {
		lookback = defaultLarkRetryLookback
	}

	builds, err := s.dbClient.DevBuild.Query().
		Where(entdevbuild.UpdatedAtGTE(time.Now().Add(-lookback))).
		All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to query builds for notification retry")
		return
	}

	failed := filterFailedNotifications(builds)
	if len(failed) == 0 {
		return
	}

	s.logger.Info().
		Int("count", len(failed)).
		Dur("lookback", lookback).
		Msg("retrying failed notifications after config reload")

	for _, build := range failed {
		newState, err := s.larkNotifier.Notify(ctx, build)
		if err != nil {
			s.logger.Err(err).Int("build_id", build.ID).Msg("failed to retry notification")
			continue
		}
		if newState == nil {
			continue
		}

		if _, err := s.dbClient.DevBuild.UpdateOneID(build.ID).
			SetNotificationState(*newState).
			Save(ctx); err != nil {
			s.logger.Err(err).Int("build_id", build.ID).Msg("failed to store notification state on retry")
			continue
		}

		s.logger.Info().Int("build_id", build.ID).Msg("notification retried successfully")
	}
}
