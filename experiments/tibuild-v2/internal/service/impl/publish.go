package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	entpublishunit "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishunit"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// Publication unit states. A unit is the aggregate of its steps.
const (
	PublishStateQueued    = "queued"
	PublishStateRunning   = "running"
	PublishStateSucceeded = "succeeded"
	// PublishStatePartial means some targets succeeded and some failed: the
	// unit needs an operator decision, not an automatic retry.
	PublishStatePartial  = "partial"
	PublishStateFailed   = "failed"
	PublishStateCanceled = "canceled"
)

// Publication step states.
const (
	PublishStepStateBlocked   = "blocked"
	PublishStepStatePending   = "pending"
	PublishStepStateClaimed   = "claimed"
	PublishStepStateSucceeded = "succeeded"
	PublishStepStateFailed    = "failed"
	PublishStepStateSkipped   = "skipped"
)

// defaultListLimit bounds GET /publish/units.
const defaultListLimit = 20

// publishsrvc is the publish service implementation. It owns both the HTTP API
// (planning and inspection) and, later, the executor of the planned steps.
type publishsrvc struct {
	logger *zerolog.Logger
	db     *ent.Client

	planner   *publishPlanner
	workerCfg config.Worker
	images    imageOps

	// lane serializes publication intents that must not overlap; only the TiUP
	// kind uses it.
	lane *publishLane
	// tiup publishes packages to a mirror.
	tiup *tiupCLI
	// publishCfg carries the publish-domain configuration used by the legacy
	// TiUP surface (mirror URLs, dependency seeding).
	publishCfg config.Publish
	// fetchConfig reads an OCI artifact config and its manifest digest. It is
	// a field so tests can replay a recorded artifact without a registry.
	fetchConfig func(ctx context.Context, artifactURL string) (map[string]any, string, error)

	nowFn func() time.Time
	newID func() string

	inflight sync.WaitGroup
}

// NewPublish returns the publish service implementation.
func NewPublish(logger *zerolog.Logger, cfg *config.Service) publish.Service {
	db, err := newStoreClient(cfg.Store)
	if err != nil {
		logger.Err(err).Msg("failed to create store client")
		return nil
	}
	return newPublishService(logger, db, cfg)
}

func newPublishService(logger *zerolog.Logger, db *ent.Client, cfg *config.Service) *publishsrvc {
	workerCfg := cfg.Worker
	workerCfg.ApplyDefaults()
	// Publication may be concentrated in the regions that can reach each
	// registry, independently from the image sync queue sharding.
	workerCfg.Targets = cfg.Publish.PublicationTargets(workerCfg.Targets)

	return &publishsrvc{
		logger: logger,
		db:     db,
		planner: &publishPlanner{
			rules:    &rulesLoader{path: cfg.Publish.DeliveryConfigPathOrDefault()},
			resolver: craneImageOps{},
		},
		workerCfg:   workerCfg,
		images:      craneImageOps{},
		lane:        newPublishLane(db, PublishLaneTiUP),
		tiup:        newTiupCLI(cfg.Publish.TiUPBinary),
		publishCfg:  cfg.Publish,
		fetchConfig: fetchTiUPArtifactConfig,
		nowFn:       time.Now,
		newID:       newUUID,
	}
}

func (s *publishsrvc) now() time.Time {
	if s.nowFn != nil {
		return s.nowFn()
	}
	return time.Now()
}

func (s *publishsrvc) id() string {
	if s.newID != nil {
		return s.newID()
	}
	return newUUID()
}

// PublishImages plans the delivery of an image and enqueues one step per
// destination repository.
//
// A reference whose rules expand to nothing (unknown repository, no matching
// tag rule or a deprecated source) is not an error: the legacy generator wrote
// no script and the task succeeded, so the response has an empty step list and
// no unit is created.
func (s *publishsrvc) PublishImages(ctx context.Context, p *publish.PublishImagesPayload) (*publish.PublishUnit, error) {
	src := ""
	if p.ArtifactURL != nil {
		src = strings.TrimSpace(*p.ArtifactURL)
	}
	if src == "" && p.Image != nil {
		src = strings.TrimSpace(*p.Image)
	}
	if src == "" {
		return nil, badRequest("artifactUrl or image is required")
	}

	plan, err := s.planner.plan(ctx, src)
	switch {
	case errors.Is(err, ErrNothingToPublish):
		s.logger.Info().Str("image", src).Msg("no delivery rule matched, nothing to publish")
		return &publish.PublishUnit{
			Kind:        PublishKindImageCopy,
			ArtifactURL: src,
			State:       publish.PublishState(PublishStateSucceeded),
			Steps:       []*publish.PublishStep{},
		}, nil
	case errors.Is(err, delivery.ErrMissingTag), errors.Is(err, delivery.ErrEmptyReference):
		return nil, badRequest(err.Error())
	case err != nil:
		var re resolveError
		if errors.As(err, &re) {
			s.logger.Err(err).Str("image", src).Msg("failed to resolve the source digest")
			return nil, badGateway(err.Error())
		}
		s.logger.Err(err).Str("image", src).Msg("failed to plan the image delivery")
		return nil, internalError(err)
	}

	unit, steps, err := s.createUnit(ctx, plan)
	if err != nil {
		s.logger.Err(err).Str("image", src).Msg("failed to enqueue the publication unit")
		return nil, internalError(err)
	}

	s.logger.Info().
		Str("unit_id", unit.ID).
		Str("image", src).
		Str("digest", plan.Digest).
		Int("steps", len(steps)).
		Msg("publication unit queued")

	return s.toPublishUnit(unit, steps), nil
}

// createUnit persists the unit and its steps atomically.
func (s *publishsrvc) createUnit(ctx context.Context, plan *publicationPlan) (*ent.PublishUnit, []*ent.PublishStep, error) {
	now := s.now()

	tx, err := s.db.Tx(ctx)
	if err != nil {
		return nil, nil, err
	}
	rollback := func(err error) error {
		if rbErr := tx.Rollback(); rbErr != nil {
			return errors.Join(err, rbErr)
		}
		return err
	}

	unit, err := tx.PublishUnit.Create().
		SetID(s.id()).
		SetKind(PublishKindImageCopy).
		SetArtifactURL(plan.ArtifactURL).
		SetDigest(plan.Digest).
		SetState(PublishStateQueued).
		SetCreatedAt(now).
		Save(ctx)
	if err != nil {
		return nil, nil, rollback(err)
	}

	creates := make([]*ent.PublishStepCreate, 0, len(plan.Steps))
	for _, step := range plan.Steps {
		payload, err := payloadMap(step.Payload)
		if err != nil {
			return nil, nil, rollback(err)
		}
		creates = append(creates, tx.PublishStep.Create().
			SetID(s.id()).
			SetUnitID(unit.ID).
			SetTarget(step.Target).
			SetDest(step.Dest).
			SetState(PublishStepStatePending).
			SetPayload(payload).
			SetCreatedAt(now))
	}
	steps, err := tx.PublishStep.CreateBulk(creates...).Save(ctx)
	if err != nil {
		return nil, nil, rollback(err)
	}

	if err := tx.Commit(); err != nil {
		return nil, nil, err
	}
	return unit, steps, nil
}

// GetUnit returns a unit with all its steps.
func (s *publishsrvc) GetUnit(ctx context.Context, p *publish.GetUnitPayload) (*publish.PublishUnit, error) {
	unit, steps, err := s.loadUnit(ctx, p.ID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, notFound(fmt.Sprintf("publish unit %s not found", p.ID))
		}
		s.logger.Err(err).Str("unit_id", p.ID).Msg("failed to read the publish unit")
		return nil, internalError(err)
	}
	return s.toPublishUnit(unit, steps), nil
}

// ListUnits lists units, newest first.
func (s *publishsrvc) ListUnits(ctx context.Context, p *publish.ListUnitsPayload) ([]*publish.PublishUnit, error) {
	limit := p.Limit
	if limit <= 0 {
		limit = defaultListLimit
	}
	if limit > 200 {
		limit = 200
	}

	query := s.db.PublishUnit.Query().
		Order(ent.Desc(entpublishunit.FieldCreatedAt), ent.Desc(entpublishunit.FieldID)).
		Limit(limit)
	if p.Status != nil {
		query = query.Where(entpublishunit.State(*p.Status))
	}
	if p.Kind != nil {
		query = query.Where(entpublishunit.Kind(*p.Kind))
	}

	units, err := query.All(ctx)
	if err != nil {
		s.logger.Err(err).Msg("failed to list the publish units")
		return nil, internalError(err)
	}

	ids := make([]string, 0, len(units))
	for _, unit := range units {
		ids = append(ids, unit.ID)
	}
	byUnit := map[string][]*ent.PublishStep{}
	if len(ids) > 0 {
		steps, err := s.db.PublishStep.Query().
			Where(entpublishstep.UnitIDIn(ids...)).
			Order(ent.Asc(entpublishstep.FieldCreatedAt), ent.Asc(entpublishstep.FieldID)).
			All(ctx)
		if err != nil {
			s.logger.Err(err).Msg("failed to read the publish steps")
			return nil, internalError(err)
		}
		for _, step := range steps {
			byUnit[step.UnitID] = append(byUnit[step.UnitID], step)
		}
	}

	out := make([]*publish.PublishUnit, 0, len(units))
	for _, unit := range units {
		out = append(out, s.toPublishUnit(unit, byUnit[unit.ID]))
	}
	return out, nil
}

// RetryUnit re-queues the failed steps of a unit. Succeeded steps are left
// alone, so a retry copies only what is missing.
func (s *publishsrvc) RetryUnit(ctx context.Context, p *publish.RetryUnitPayload) (*publish.PublishUnit, error) {
	unit, steps, err := s.loadUnit(ctx, p.ID)
	if err != nil {
		if ent.IsNotFound(err) {
			return nil, notFound(fmt.Sprintf("publish unit %s not found", p.ID))
		}
		return nil, internalError(err)
	}

	retryable := 0
	for _, step := range steps {
		if step.State == PublishStepStateFailed || step.State == PublishStepStateSkipped {
			retryable++
		}
	}
	if retryable == 0 {
		return nil, badRequest(fmt.Sprintf("publish unit %s has no failed step to retry", unit.ID))
	}

	now := s.now()
	if _, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.UnitID(unit.ID),
			entpublishstep.StateIn(PublishStepStateFailed, PublishStepStateSkipped),
		).
		// Blocked first: an ordered unit must not let a later step overtake the
		// predecessor it depends on. The promotion below releases the runnable
		// ones.
		SetState(PublishStepStateBlocked).
		SetAttempts(0).
		ClearClaimedBy().
		ClearLeaseUntil().
		ClearAvailableAt().
		ClearLastError().
		ClearStartedAt().
		ClearFinishedAt().
		Save(ctx); err != nil {
		s.logger.Err(err).Str("unit_id", unit.ID).Msg("failed to re-queue the failed steps")
		return nil, internalError(err)
	}

	if err := s.promoteRunnableSteps(ctx, unit.ID); err != nil {
		s.logger.Err(err).Str("unit_id", unit.ID).Msg("failed to promote the runnable steps")
		return nil, internalError(err)
	}

	updated, err := s.db.PublishUnit.UpdateOneID(unit.ID).
		SetState(PublishStateQueued).
		ClearFinishedAt().
		Save(ctx)
	if err != nil {
		s.logger.Err(err).Str("unit_id", unit.ID).Msg("failed to re-queue the publish unit")
		return nil, internalError(err)
	}

	s.logger.Info().
		Str("unit_id", unit.ID).
		Int("steps", retryable).
		Time("at", now).
		Msg("publish unit re-queued")

	steps, err = s.loadSteps(ctx, unit.ID)
	if err != nil {
		return nil, internalError(err)
	}
	return s.toPublishUnit(updated, steps), nil
}

func (s *publishsrvc) loadUnit(ctx context.Context, id string) (*ent.PublishUnit, []*ent.PublishStep, error) {
	unit, err := s.db.PublishUnit.Get(ctx, id)
	if err != nil {
		return nil, nil, err
	}
	steps, err := s.loadSteps(ctx, id)
	if err != nil {
		return nil, nil, err
	}
	return unit, steps, nil
}

func (s *publishsrvc) loadSteps(ctx context.Context, unitID string) ([]*ent.PublishStep, error) {
	return s.db.PublishStep.Query().
		Where(entpublishstep.UnitID(unitID)).
		Order(ent.Asc(entpublishstep.FieldCreatedAt), ent.Asc(entpublishstep.FieldID)).
		All(ctx)
}

// toPublishUnit converts a stored unit and its steps into the API type.
func (s *publishsrvc) toPublishUnit(unit *ent.PublishUnit, steps []*ent.PublishStep) *publish.PublishUnit {
	out := &publish.PublishUnit{
		ID:          unit.ID,
		Kind:        unit.Kind,
		ArtifactURL: unit.ArtifactURL,
		State:       publish.PublishState(unit.State),
		CreatedAt:   formatTime(unit.CreatedAt),
		Steps:       make([]*publish.PublishStep, 0, len(steps)),
	}
	if unit.Digest != "" {
		out.Digest = stringPtr(unit.Digest)
	}
	if unit.RequestedBy != "" {
		out.RequestedBy = stringPtr(unit.RequestedBy)
	}
	if unit.FinishedAt != nil {
		out.FinishedAt = formatTime(*unit.FinishedAt)
	}
	for _, step := range steps {
		out.Steps = append(out.Steps, toPublishStep(step))
	}
	return out
}

func toPublishStep(step *ent.PublishStep) *publish.PublishStep {
	out := &publish.PublishStep{
		ID:      step.ID,
		UnitID:  step.UnitID,
		Target:  step.Target,
		State:   publish.PublishStepState(step.State),
		Payload: step.Payload,
	}
	if step.Dest != "" {
		out.Dest = stringPtr(step.Dest)
	}
	if step.DependsOn != "" {
		out.DependsOn = stringPtr(step.DependsOn)
	}
	if step.Attempts != 0 {
		out.Attempts = intPtr(step.Attempts)
	}
	if step.LastError != "" {
		out.LastError = stringPtr(step.LastError)
	}
	if len(step.Result) > 0 {
		out.Result = step.Result
	}
	out.CreatedAt = formatTime(step.CreatedAt)
	if step.StartedAt != nil {
		out.StartedAt = formatTime(*step.StartedAt)
	}
	if step.FinishedAt != nil {
		out.FinishedAt = formatTime(*step.FinishedAt)
	}
	return out
}

func stringPtr(v string) *string { return &v }
func intPtr(v int) *int          { return &v }

func badRequest(message string) error {
	return &publish.PublishBadRequestError{Code: http.StatusBadRequest, Message: message}
}

func notFound(message string) error {
	return &publish.PublishNotFoundError{Code: http.StatusNotFound, Message: message}
}

func badGateway(message string) error {
	return &publish.PublishBadGatewayError{Code: http.StatusBadGateway, Message: message}
}

func internalError(err error) error {
	return &publish.PublishInternalServerError{Code: http.StatusInternalServerError, Message: err.Error()}
}
