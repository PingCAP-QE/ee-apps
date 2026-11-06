package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	entpublishunit "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishunit"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// tiupChannel is the identity of one TiUP publish channel: the set of target
// steps and the exact package set. Two units with the same channel publish the
// same files to the same mirrors, so the newer one can replace the older.
//
// A package-set match is deliberate: cancelling a unit because one package
// overlaps would drop the other packages it carries.
type tiupChannel struct {
	Targets  string
	Packages string
}

func tiupChannelForPlans(plans []delivery.TiUPPlan) tiupChannel {
	targets := make([]string, 0, len(plans))
	packages := make([]string, 0)
	for _, plan := range plans {
		targets = append(targets, plan.Target)
		for _, pkg := range plan.Packages {
			packages = append(packages, strings.Join([]string{
				plan.Target, pkg.Name, pkg.Version, pkg.OS, pkg.Arch,
			}, "\x00"))
		}
	}
	sort.Strings(targets)
	sort.Strings(packages)
	return tiupChannel{
		Targets:  strings.Join(targets, "\x00"),
		Packages: strings.Join(packages, "\x00"),
	}
}

func tiupChannelForUnit(unit *ent.PublishUnit, steps []*ent.PublishStep) (tiupChannel, bool) {
	targets := make([]string, 0, len(steps))
	packages := make([]string, 0)
	for _, step := range steps {
		payload, err := decodeTiUPStepPayload(step.Payload)
		if err != nil {
			return tiupChannel{}, false
		}
		targets = append(targets, step.Target)
		for _, pkg := range payload.Packages {
			packages = append(packages, strings.Join([]string{
				step.Target, pkg.Name, pkg.Version, pkg.OS, pkg.Arch,
			}, "\x00"))
		}
	}
	sort.Strings(targets)
	sort.Strings(packages)
	return tiupChannel{
		Targets:  strings.Join(targets, "\x00"),
		Packages: strings.Join(packages, "\x00"),
	}, true
}

type tiupCandidate struct {
	unit      *ent.PublishUnit
	steps     []*ent.PublishStep
	createdAt time.Time
}

// findTiUPChannel returns the queued/running immediate units that publish the
// same channel. Nightly units are excluded: their latest-wins schedule lives in
// publish_desired.
func (s *publishsrvc) findTiUPChannel(ctx context.Context, channel tiupChannel) ([]tiupCandidate, error) {
	units, err := s.db.PublishUnit.Query().
		Where(
			entpublishunit.StateIn(PublishStateQueued, PublishStateRunning),
			entpublishunit.SourceNEQ(schema.PublishSourceNightly),
		).
		All(ctx)
	if err != nil {
		return nil, err
	}

	candidates := make([]tiupCandidate, 0, len(units))
	for _, unit := range units {
		steps, err := s.db.PublishStep.Query().
			Where(entpublishstep.UnitID(unit.ID)).
			All(ctx)
		if err != nil {
			return nil, err
		}
		key, ok := tiupChannelForUnit(unit, steps)
		if !ok || key != channel {
			continue
		}
		candidates = append(candidates, tiupCandidate{
			unit:      unit,
			steps:     steps,
			createdAt: tiupUnitArtifactTime(unit, steps),
		})
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].createdAt.Equal(candidates[j].createdAt) {
			return candidates[i].unit.CreatedAt.After(candidates[j].unit.CreatedAt)
		}
		return candidates[i].createdAt.After(candidates[j].createdAt)
	})
	return candidates, nil
}

// tiupUnitArtifactTime returns the artifact build time recorded in the step
// payload, falling back to the queue time for units created before the field
// existed.
func tiupUnitArtifactTime(unit *ent.PublishUnit, steps []*ent.PublishStep) time.Time {
	for _, step := range steps {
		payload, err := decodeTiUPStepPayload(step.Payload)
		if err != nil {
			continue
		}
		if !payload.ArtifactCreatedAt.IsZero() {
			return payload.ArtifactCreatedAt
		}
	}
	return unit.CreatedAt
}

// supersedeTiUPUnit applies the queue policy for immediate TiUP publications:
//
//   - same digest: return the existing unit (idempotent);
//   - non-formal, new artifact is newer: create the new unit and cancel the
//     older idle units (step.last_error records the supersession);
//   - formal vX.Y.Z, different digest: return 409 without creating or
//     cancelling anything.
//
// The returned reused flag tells the caller to return the existing step ids.
func (s *publishsrvc) supersedeTiUPUnit(
	ctx context.Context,
	artifactURL, requestedBy, digest string,
	artifactCreatedAt time.Time,
	plans []delivery.TiUPPlan,
) (*ent.PublishUnit, []*ent.PublishStep, bool, error) {
	channel := tiupChannelForPlans(plans)
	candidates, err := s.findTiUPChannel(ctx, channel)
	if err != nil {
		return nil, nil, false, err
	}

	newTime := artifactCreatedAt
	if newTime.IsZero() {
		newTime = s.now()
	}

	formal := tiupPlansHaveFormalVersion(plans)

	// A delayed retry of an older artifact must not replace a newer one. This
	// check runs before the same-digest shortcut: the newest queued unit wins
	// even when the stale request happens to match an older unit's digest.
	if len(candidates) > 0 &&
		candidates[0].createdAt.After(newTime) &&
		candidates[0].unit.Digest != digest {
		if formal {
			return nil, nil, false, s.staleFormalConflict(ctx, channel, candidates[0], digest, newTime)
		}
		s.logger.Info().
			Str("artifact", artifactURL).
			Str("existing_unit", candidates[0].unit.ID).
			Time("existing_artifact_time", candidates[0].createdAt).
			Time("new_artifact_time", newTime).
			Msg("stale artifact ignored; a newer unit is already queued")
		return candidates[0].unit, candidates[0].steps, true, nil
	}

	// Same digest is the same content: no new work.
	for _, candidate := range candidates {
		if candidate.unit.Digest == digest {
			return candidate.unit, candidate.steps, true, nil
		}
	}

	if formal && len(candidates) > 0 {
		return nil, nil, false, s.formalConflict(ctx, channel, candidates[0], digest)
	}

	unit, steps, err := s.createTiUPUnit(ctx, artifactURL, requestedBy, digest, artifactCreatedAt, plans)
	if err != nil {
		return nil, nil, false, err
	}

	for _, candidate := range candidates {
		if candidate.unit.Digest == digest {
			continue
		}
		if candidate.createdAt.After(newTime) {
			continue
		}
		cancelled, err := s.cancelIdleUnit(ctx, candidate.unit.ID, unit.ID, s.now())
		if err != nil {
			s.logger.Err(err).Str("unit_id", candidate.unit.ID).Msg("failed to cancel a superseded publish unit")
			continue
		}
		if cancelled {
			s.logger.Info().
				Str("unit_id", candidate.unit.ID).
				Str("superseded_by", unit.ID).
				Msg("publish unit superseded")
		}
	}
	return unit, steps, false, nil
}

func tiupPlansHaveFormalVersion(plans []delivery.TiUPPlan) bool {
	for _, plan := range plans {
		for _, pkg := range plan.Packages {
			if isFormalTiUPVersion(pkg.Version) {
				return true
			}
		}
	}
	return false
}

// tiupPublishError keeps a typed conflict error (mapped to 409 by the HTTP
// transport) and wraps every other enqueue failure as a TiUP fault.
func tiupPublishError(err error) error {
	var conflict *publish.PublishConflictError
	if errors.As(err, &conflict) {
		return err
	}
	return publishFault(err)
}

func (s *publishsrvc) formalConflict(ctx context.Context, channel tiupChannel, candidate tiupCandidate, newDigest string) error {
	version := tiupCandidateVersion(candidate)
	existingDigest := candidate.unit.Digest
	message := fmt.Sprintf(
		"formal version %s conflicts with queued unit %s: existing digest %s, new digest %s",
		version, candidate.unit.ID, existingDigest, newDigest,
	)
	s.sendConflictAlert(ctx, channel, version, existingDigest, newDigest, candidate.unit.ID)
	return &publish.PublishConflictError{
		Code:           http.StatusConflict,
		Message:        message,
		Version:        &version,
		ExistingDigest: &existingDigest,
		NewDigest:      &newDigest,
		ExistingUnitID: &candidate.unit.ID,
	}
}

func (s *publishsrvc) staleFormalConflict(ctx context.Context, channel tiupChannel, candidate tiupCandidate, newDigest string, newTime time.Time) error {
	version := tiupCandidateVersion(candidate)
	existingDigest := candidate.unit.Digest
	message := fmt.Sprintf(
		"stale formal artifact for %s: queued unit %s has digest %s built at %s; new digest %s was built at %s",
		version, candidate.unit.ID, existingDigest,
		candidate.createdAt.UTC().Format(time.RFC3339),
		newDigest, newTime.UTC().Format(time.RFC3339),
	)
	s.sendConflictAlert(ctx, channel, version, existingDigest, newDigest, candidate.unit.ID)
	return &publish.PublishConflictError{
		Code:           http.StatusConflict,
		Message:        message,
		Version:        &version,
		ExistingDigest: &existingDigest,
		NewDigest:      &newDigest,
		ExistingUnitID: &candidate.unit.ID,
	}
}

func tiupCandidateVersion(candidate tiupCandidate) string {
	for _, step := range candidate.steps {
		payload, err := decodeTiUPStepPayload(step.Payload)
		if err != nil {
			continue
		}
		for _, pkg := range payload.Packages {
			if isFormalTiUPVersion(pkg.Version) {
				return pkg.Version
			}
		}
	}
	return ""
}

func (s *publishsrvc) sendConflictAlert(ctx context.Context, channel tiupChannel, version, existingDigest, newDigest, unitID string) {
	if s.alerter == nil || s.conflictAlerts == nil {
		return
	}
	key := "formal-conflict/" + channel.Targets + "/" + channel.Packages
	if !s.conflictAlerts.shouldSend(key, s.now()) {
		return
	}
	body := fmt.Sprintf(
		"version=%s existing_unit=%s existing_digest=%s new_digest=%s",
		version, unitID, existingDigest, newDigest,
	)
	if err := s.alerter.SendAlert(ctx, "[tibuild] formal version conflict", body); err != nil {
		s.logger.Err(err).Str("key", key).Msg("failed to send the formal version conflict alert")
	}
}

// cancelIdleUnit cancels a queued/running unit that has not started any
// irreversible work. A unit with a claimed or succeeded step is left alone:
// the new unit will publish after it, and the formal guard decides what is
// allowed.
func (s *publishsrvc) cancelIdleUnit(ctx context.Context, unitID, newUnitID string, now time.Time) (bool, error) {
	tx, err := s.db.Tx(ctx)
	if err != nil {
		return false, err
	}
	rollback := func(err error) (bool, error) {
		_ = tx.Rollback()
		return false, err
	}

	steps, err := tx.PublishStep.Query().
		Where(entpublishstep.UnitID(unitID)).
		All(ctx)
	if err != nil {
		return rollback(err)
	}
	for _, step := range steps {
		if step.State == PublishStepStateClaimed || step.State == PublishStepStateSucceeded {
			return rollback(nil)
		}
	}

	if _, err := tx.PublishStep.Update().
		Where(
			entpublishstep.UnitID(unitID),
			entpublishstep.StateIn(PublishStepStatePending, PublishStepStateBlocked),
		).
		SetState(PublishStepStateSkipped).
		SetLastError("superseded by " + newUnitID).
		SetFinishedAt(now).
		Save(ctx); err != nil {
		return rollback(err)
	}

	// A step may have been claimed between the read and the update above.
	active, err := tx.PublishStep.Query().
		Where(
			entpublishstep.UnitID(unitID),
			entpublishstep.StateIn(PublishStepStateClaimed, PublishStepStateSucceeded),
		).
		Count(ctx)
	if err != nil {
		return rollback(err)
	}
	if active > 0 {
		return rollback(nil)
	}

	if _, err := tx.PublishUnit.UpdateOneID(unitID).
		SetState(PublishStateCanceled).
		SetFinishedAt(now).
		Save(ctx); err != nil {
		return rollback(err)
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	return true, nil
}

// conflictAlertTracker deduplicates formal-conflict alerts per channel.
type conflictAlertTracker struct {
	mu     sync.Mutex
	dedupe time.Duration
	last   map[string]time.Time
}

func newConflictAlertTracker(dedupe time.Duration) *conflictAlertTracker {
	if dedupe <= 0 {
		dedupe = time.Hour
	}
	return &conflictAlertTracker{dedupe: dedupe, last: map[string]time.Time{}}
}

func (a *conflictAlertTracker) shouldSend(key string, now time.Time) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if last, ok := a.last[key]; ok && now.Sub(last) < a.dedupe {
		return false
	}
	a.last[key] = now
	return true
}
