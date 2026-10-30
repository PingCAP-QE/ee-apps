package impl

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/predicate"
	entpublishdesired "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishdesired"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// desiredKey is the logical identity of one nightly channel.
type desiredKey struct {
	Target string
	Pkg    string
	OS     string
	Arch   string
}

// idleDesired matches rows with no in-flight unit. The column is nullable, so
// an unset value is NULL, not the empty string.
func idleDesired() predicate.PublishDesired {
	return entpublishdesired.Or(
		entpublishdesired.RunningUnitIDIsNil(),
		entpublishdesired.RunningUnitID(""),
	)
}

func (k desiredKey) predicates() []predicate.PublishDesired {
	return []predicate.PublishDesired{
		entpublishdesired.Target(k.Target),
		entpublishdesired.Pkg(k.Pkg),
		entpublishdesired.Os(k.OS),
		entpublishdesired.Arch(k.Arch),
	}
}

// desiredClaim is one row a tick has reserved for publication. The unit id is
// written before the unit exists so two workers cannot both publish the row;
// a creation failure releases the claim.
type desiredClaim struct {
	Row    *ent.PublishDesired
	UnitID string
}

// desiredStore owns the publish_desired table: the "latest wins" inbox, the
// window-aligned schedule and the conditional claim.
type desiredStore struct {
	db    *ent.Client
	nowFn func() time.Time
	newID func() string

	window     time.Duration
	anchor     time.Duration
	loc        *time.Location
	jitter     time.Duration
	retryAfter time.Duration
	batch      int
}

func newDesiredStore(db *ent.Client, cfg config.NightlyPublish) *desiredStore {
	return &desiredStore{
		db:         db,
		nowFn:      time.Now,
		newID:      newUUID,
		window:     cfg.WindowDuration(),
		anchor:     cfg.AnchorOffset(),
		loc:        cfg.Location(),
		jitter:     cfg.JitterDuration(),
		retryAfter: cfg.RetryAfterDuration(),
		batch:      cfg.BatchSize(),
	}
}

func (s *desiredStore) now() time.Time {
	if s.nowFn != nil {
		return s.nowFn()
	}
	return time.Now()
}

func (s *desiredStore) id() string {
	if s.newID != nil {
		return s.newID()
	}
	return newUUID()
}

// nextRunAt returns the first window boundary strictly after now, shifted by a
// stable per-package jitter. The anchor is interpreted in the configured zone;
// the result is normalised to UTC so the database stores one instant.
func (s *desiredStore) nextRunAt(now time.Time, pkg string) time.Time {
	local := now.In(s.loc)
	day := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, s.loc)
	candidate := day.Add(s.anchor)
	for !candidate.After(now) {
		candidate = candidate.Add(s.window)
	}
	return candidate.Add(s.jitterFor(pkg)).UTC()
}

// jitterFor spreads packages that share a window boundary. It is stable across
// processes and ticks because it hashes the package name, not the clock.
func (s *desiredStore) jitterFor(pkg string) time.Duration {
	if s.jitter <= 0 {
		return 0
	}
	sum := sha256.Sum256([]byte(pkg))
	n := binary.BigEndian.Uint64(sum[:8])
	return time.Duration(n % uint64(s.jitter))
}

// Upsert records the newest desired artifact. It never touches next_run_at or
// running_unit_id: those belong to the schedule and to the in-flight unit, so a
// push that arrives during a publication waits for the next tick.
//
// Ent has no ON DUPLICATE KEY UPDATE and the tests run on SQLite, so this is an
// update-then-insert with a duplicate-key retry. Both statements protect the
// same invariant: the row's desired fields are replaced, its schedule is not.
func (s *desiredStore) Upsert(ctx context.Context, key desiredKey, version string, payload map[string]any, now time.Time) (*ent.PublishDesired, error) {
	if _, err := s.db.PublishDesired.Update().
		Where(key.predicates()...).
		SetDesiredVersion(version).
		SetDesiredPayload(payload).
		SetDesiredAt(now).
		SetUpdatedAt(now).
		Save(ctx); err != nil {
		return nil, err
	}

	row, err := s.db.PublishDesired.Query().Where(key.predicates()...).Only(ctx)
	if err == nil {
		return row, nil
	}
	if !ent.IsNotFound(err) {
		return nil, err
	}

	row, err = s.db.PublishDesired.Create().
		SetTarget(key.Target).
		SetPkg(key.Pkg).
		SetOs(key.OS).
		SetArch(key.Arch).
		SetDesiredVersion(version).
		SetDesiredPayload(payload).
		SetDesiredAt(now).
		SetNextRunAt(s.nextRunAt(now, key.Pkg)).
		SetCreatedAt(now).
		SetUpdatedAt(now).
		Save(ctx)
	if err == nil {
		return row, nil
	}
	if !ent.IsConstraintError(err) {
		return nil, err
	}
	// Another worker inserted the same channel between our update and create;
	// its row is the desired one.
	return s.db.PublishDesired.Query().Where(key.predicates()...).Only(ctx)
}

// ClaimDue reserves up to one batch of due, idle rows for the given targets.
// The conditional UPDATE is the multi-worker guard: a row is claimed exactly
// once even when several workers select it.
func (s *desiredStore) ClaimDue(ctx context.Context, targets []string, now time.Time) ([]*desiredClaim, error) {
	query := s.db.PublishDesired.Query().
		Where(
			idleDesired(),
			entpublishdesired.NextRunAtLTE(now),
		)
	if !hasWildcardTarget(targets) && len(targets) > 0 {
		query = query.Where(entpublishdesired.TargetIn(targets...))
	}
	rows, err := query.
		Order(entpublishdesired.ByNextRunAt(), entpublishdesired.ByID()).
		Limit(s.batch).
		All(ctx)
	if err != nil {
		return nil, err
	}

	claims := make([]*desiredClaim, 0, len(rows))
	for _, row := range rows {
		unitID := s.id()
		key := desiredKey{Target: row.Target, Pkg: row.Pkg, OS: row.Os, Arch: row.Arch}
		updated, err := s.db.PublishDesired.Update().
			Where(append(key.predicates(),
				idleDesired(),
				entpublishdesired.NextRunAtLTE(now),
			)...).
			SetRunningUnitID(unitID).
			SetUpdatedAt(now).
			Save(ctx)
		if err != nil {
			return claims, err
		}
		if updated != 1 {
			continue
		}
		claimed, err := s.db.PublishDesired.Query().
			Where(append(key.predicates(), entpublishdesired.RunningUnitID(unitID))...).
			Only(ctx)
		if err != nil {
			return claims, err
		}
		claims = append(claims, &desiredClaim{Row: claimed, UnitID: unitID})
	}
	return claims, nil
}

// Release returns a claimed row to the schedule after the unit could not be
// created. It is fenced on the claimed unit id.
func (s *desiredStore) Release(ctx context.Context, key desiredKey, unitID string, cause error, now time.Time) error {
	update := s.db.PublishDesired.Update().
		Where(append(key.predicates(), entpublishdesired.RunningUnitID(unitID))...).
		ClearRunningUnitID().
		SetNextRunAt(now.Add(s.retryAfter)).
		SetUpdatedAt(now)
	if cause != nil {
		update = update.SetLastError(truncateErrorMessage(cause.Error()))
	}
	_, err := update.Save(ctx)
	return err
}

// Settle closes every desired row currently claimed by unitID. It is called
// from the unit aggregator and, as a safety sweep, from every tick.
func (s *desiredStore) Settle(ctx context.Context, unitID, state, version, detail string, now time.Time) error {
	rows, err := s.db.PublishDesired.Query().
		Where(entpublishdesired.RunningUnitID(unitID)).
		All(ctx)
	if err != nil {
		return err
	}
	for _, row := range rows {
		key := desiredKey{Target: row.Target, Pkg: row.Pkg, OS: row.Os, Arch: row.Arch}
		if err := s.completeRow(ctx, row, key, unitID, state, version, detail, now); err != nil {
			return err
		}
	}
	return nil
}

// completeRow advances the schedule and records the outcome.
//
// Success records the published version and moves to the next aligned window.
// A failure keeps desired_version untouched, so the next window retries the
// same newest artifact; the first failure of a version gets a short backoff,
// later failures fall back to the window to avoid a hot retry loop.
func (s *desiredStore) completeRow(ctx context.Context, row *ent.PublishDesired, key desiredKey, unitID, state, version, detail string, now time.Time) error {
	if version == "" {
		version = row.DesiredVersion
	}
	update := s.db.PublishDesired.Update().
		Where(append(key.predicates(), entpublishdesired.RunningUnitID(unitID))...).
		ClearRunningUnitID().
		SetUpdatedAt(now)

	if state == PublishStateSucceeded {
		update = update.
			SetLastSucceededVersion(version).
			ClearLastError().
			SetNextRunAt(s.nextRunAt(now, row.Pkg))
	} else {
		next := now.Add(s.retryAfter)
		if row.LastFailedVersion == version {
			next = s.nextRunAt(now, row.Pkg)
		}
		if detail == "" {
			detail = fmt.Sprintf("publish unit finished in state %s", state)
		}
		update = update.
			SetLastFailedVersion(version).
			SetLastError(truncateErrorMessage(detail)).
			SetNextRunAt(next)
	}

	_, err := update.Save(ctx)
	return err
}

// Get returns one desired row, used by tests and by the read-only API.
func (s *desiredStore) Get(ctx context.Context, key desiredKey) (*ent.PublishDesired, error) {
	row, err := s.db.PublishDesired.Query().Where(key.predicates()...).Only(ctx)
	if err != nil {
		return nil, err
	}
	return row, nil
}

// nightlyEnabled reports whether this worker should run the reconciler.
func (s *publishsrvc) nightlyEnabled() bool {
	if s.desired == nil || !s.publishCfg.Nightly.EnabledValue() {
		return false
	}
	return len(s.nightlyTargets()) > 0
}

// nightlyTargets resolves the targets that converge on this worker. The
// publish targets, not worker.targets, are authoritative: the two are separated
// so concentrating publication does not re-shard the image sync queue.
func (s *publishsrvc) nightlyTargets() []string {
	return s.publishCfg.Nightly.DesiredTargets(
		s.publishCfg.PublicationTargets(s.workerCfg.Targets))
}

// enqueueNightlyPlans records the newest artifact for every nightly plan. It
// creates no unit: the window-aligned tick does that later.
func (s *publishsrvc) enqueueNightlyPlans(ctx context.Context, artifactURL, digest string, plans []delivery.TiUPPlan) error {
	mirrors, err := s.mirrorURLs()
	if err != nil {
		return err
	}
	now := s.now()

	for _, plan := range plans {
		if !plan.Nightly {
			continue
		}
		for _, pkg := range plan.Packages {
			payload := TiUPStepPayload{
				Mirror:         plan.Mirror,
				MirrorURL:      mirrors[plan.Mirror],
				ArtifactURL:    artifactURL,
				Digest:         digest,
				Nightly:        true,
				NightlyVersion: plan.Version,
				Packages:       []delivery.TiUPPackage{pkg},
			}
			raw, err := payloadMap(payload)
			if err != nil {
				return err
			}
			key := desiredKey{Target: plan.Target, Pkg: pkg.Name, OS: pkg.OS, Arch: pkg.Arch}
			if _, err := s.desired.Upsert(ctx, key, plan.Version, raw, now); err != nil {
				return fmt.Errorf("upsert nightly desired %s/%s/%s/%s: %w", key.Target, key.Pkg, key.OS, key.Arch, err)
			}
		}
	}
	return nil
}

// runNightlyReconciler drives the window-aligned tick.
func (s *publishsrvc) runNightlyReconciler(ctx context.Context) {
	interval := s.publishCfg.Nightly.TickEvery()
	if interval <= 0 {
		interval = time.Minute
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	s.logger.Info().
		Dur("tick_interval", interval).
		Strs("targets", s.nightlyTargets()).
		Msg("nightly desired-state reconciler started")

	for {
		select {
		case <-ctx.Done():
			s.logger.Info().Msg("nightly desired-state reconciler stopped")
			return
		case <-ticker.C:
			created, err := s.reconcileNightlyOnce(ctx)
			if err != nil {
				s.logger.Err(err).Msg("nightly reconcile failed")
				continue
			}
			if created > 0 {
				s.logger.Info().Int("units", created).Msg("nightly reconcile queued publication units")
			}
		}
	}
}

// reconcileNightlyOnce settles finished claims, then claims due rows and turns
// each into a single-target publish unit.
func (s *publishsrvc) reconcileNightlyOnce(ctx context.Context) (int, error) {
	if s.desired == nil {
		return 0, nil
	}
	// Safety sweep: a worker may have died between the unit reaching a terminal
	// state and the completion callback.
	s.settleNightlyClaims(ctx)

	claims, err := s.desired.ClaimDue(ctx, s.nightlyTargets(), s.now())
	if err != nil {
		return 0, err
	}

	created := 0
	for _, claim := range claims {
		key := desiredKey{
			Target: claim.Row.Target,
			Pkg:    claim.Row.Pkg,
			OS:     claim.Row.Os,
			Arch:   claim.Row.Arch,
		}
		payload, err := decodeTiUPStepPayload(claim.Row.DesiredPayload)
		if err != nil {
			_ = s.desired.Release(ctx, key, claim.UnitID, fmt.Errorf("invalid desired payload: %w", err), s.now())
			continue
		}
		if payload.ArtifactURL == "" || payload.Mirror == "" {
			_ = s.desired.Release(ctx, key, claim.UnitID, errors.New("desired payload has no artifact_url or mirror"), s.now())
			continue
		}
		if target := tiupTargetForMirrorName(payload.Mirror); target != claim.Row.Target {
			_ = s.desired.Release(ctx, key, claim.UnitID,
				fmt.Errorf("desired target %s does not match mirror %s", claim.Row.Target, payload.Mirror), s.now())
			continue
		}

		unit, step, err := s.createSingleStepUnitWithID(ctx, claim.UnitID, payload.ArtifactURL, "", "nightly", payload)
		if err != nil {
			_ = s.desired.Release(ctx, key, claim.UnitID, err, s.now())
			s.logger.Err(err).
				Str("target", key.Target).Str("pkg", key.Pkg).
				Str("os", key.OS).Str("arch", key.Arch).
				Msg("failed to create the nightly publish unit")
			continue
		}
		created++
		s.logger.Info().
			Str("unit_id", unit.ID).
			Str("step_id", step.ID).
			Str("target", key.Target).
			Str("pkg", key.Pkg).
			Str("version", claim.Row.DesiredVersion).
			Msg("nightly desired-state unit queued")
	}
	return created, nil
}

// settleNightlyClaims closes rows whose unit already finished but whose
// completion callback was missed (worker restart, crash after the step write).
func (s *publishsrvc) settleNightlyClaims(ctx context.Context) {
	rows, err := s.db.PublishDesired.Query().
		Where(entpublishdesired.RunningUnitIDNEQ("")).
		Limit(100).
		All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to scan nightlies with a running unit")
		return
	}
	for _, row := range rows {
		unit, err := s.db.PublishUnit.Get(ctx, row.RunningUnitID)
		if ent.IsNotFound(err) {
			key := desiredKey{Target: row.Target, Pkg: row.Pkg, OS: row.Os, Arch: row.Arch}
			_ = s.desired.Release(ctx, key, row.RunningUnitID,
				fmt.Errorf("publish unit %s disappeared", row.RunningUnitID), s.now())
			continue
		}
		if err != nil || !isTerminalUnitState(unit.State) {
			continue
		}
		s.settleNightlyUnit(ctx, unit.ID)
	}
}

// settleNightlyUnit records the outcome of a terminal unit on its desired rows.
func (s *publishsrvc) settleNightlyUnit(ctx context.Context, unitID string) {
	if s.desired == nil {
		return
	}
	unit, err := s.db.PublishUnit.Get(ctx, unitID)
	if err != nil || !isTerminalUnitState(unit.State) {
		return
	}
	steps, err := s.loadSteps(ctx, unitID)
	if err != nil {
		return
	}

	version, detail := "", ""
	for _, step := range steps {
		if step.LastError != "" {
			detail = step.LastError
		}
		payload, err := decodeTiUPStepPayload(step.Payload)
		if err == nil && payload.NightlyVersion != "" {
			version = payload.NightlyVersion
		}
	}

	if err := s.desired.Settle(ctx, unitID, unit.State, version, detail, s.now()); err != nil {
		s.logger.Err(err).Str("unit_id", unitID).Msg("failed to settle the nightly desired rows")
	}
}
