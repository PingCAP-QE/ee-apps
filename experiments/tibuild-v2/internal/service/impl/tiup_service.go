package impl

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry/remote"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// DeliveryByRules publishes an artifact to every mirror the rules select.
func (s *publishsrvc) DeliveryByRules(ctx context.Context, p *publish.DeliveryByRulesPayload) ([]string, error) {
	if p == nil || strings.TrimSpace(p.ArtifactURL) == "" {
		return nil, badRequest("artifact_url is required")
	}
	src := strings.TrimSpace(p.ArtifactURL)

	rules, err := s.planner.rules.config()
	if err != nil {
		return nil, publishFault(err)
	}

	// Match first: an artifact that no rule selects must not trigger a registry
	// call (the legacy service returned an empty list).
	if targets, err := rules.MatchTiUP(src); err != nil {
		return nil, badRequest(err.Error())
	} else if len(targets) == 0 {
		s.logger.Info().Str("artifact", src).Msg("no tiup delivery rule matched")
		return []string{}, nil
	}

	artifactConfig, digest, err := s.fetchConfig(ctx, src)
	if err != nil {
		return nil, publishFault(fmt.Errorf("read artifact config: %w", err))
	}

	plans, err := rules.PlanTiUP(src, artifactConfig)
	if err != nil {
		if errors.Is(err, delivery.ErrNoTiUPArtifact) {
			s.logger.Info().Str("artifact", src).Msg("artifact has no tiup packages")
			return []string{}, nil
		}
		return nil, badRequest(err.Error())
	}
	if len(plans) == 0 {
		return []string{}, nil
	}

	// Nightly targets that converge accept the push without publishing it: the
	// newest row is upserted and the window-aligned tick publishes it later.
	// Targets that are not enabled keep the immediate path, which is what makes
	// the staging-first rollout possible.
	publishTargets := s.publishCfg.PublicationTargets(s.workerCfg.Targets)
	immediate := make([]delivery.TiUPPlan, 0, len(plans))
	deferred := make([]delivery.TiUPPlan, 0, len(plans))
	for _, plan := range plans {
		if plan.Nightly && s.publishCfg.Nightly.WantsTarget(plan.Target, publishTargets) {
			deferred = append(deferred, plan)
			continue
		}
		immediate = append(immediate, plan)
	}

	if len(deferred) > 0 {
		if err := s.enqueueNightlyPlans(ctx, src, digest, deferred); err != nil {
			return nil, publishFault(err)
		}
		s.logger.Info().
			Str("artifact", src).
			Int("targets", len(deferred)).
			Msg("nightly desired state upserted")
	}

	if len(immediate) == 0 {
		return []string{}, nil
	}

	_, steps, err := s.createTiUPUnit(ctx, src, "delivery-by-rules", digest, immediate)
	if err != nil {
		return nil, publishFault(err)
	}

	ids := make([]string, 0, len(steps))
	for _, step := range steps {
		ids = append(ids, step.ID)
	}
	s.logger.Info().
		Str("artifact", src).
		Int("steps", len(steps)).
		Msg("tiup publication unit queued")
	return ids, nil
}

// RequestToPublish publishes an artifact to one explicit mirror.
func (s *publishsrvc) RequestToPublish(ctx context.Context, p *publish.RequestToPublishPayload) ([]string, error) {
	if p == nil || strings.TrimSpace(p.ArtifactURL) == "" {
		return nil, badRequest("artifact_url is required")
	}
	src := strings.TrimSpace(p.ArtifactURL)

	mirror := p.TiupMirror
	if mirror == "" {
		mirror = delivery.TiUPMirrorStaging
	}
	target, err := tiupTargetForMirror(mirror)
	if err != nil {
		return nil, badRequest(err.Error())
	}

	artifactConfig, digest, err := s.fetchConfig(ctx, src)
	if err != nil {
		return nil, publishFault(fmt.Errorf("read artifact config: %w", err))
	}

	packages, err := delivery.TiUPPackages(artifactConfig, tagOf(src))
	if err != nil {
		if errors.Is(err, delivery.ErrNoTiUPArtifact) {
			return []string{}, nil
		}
		return nil, badRequest(err.Error())
	}

	version := ""
	if p.Version != nil {
		version = strings.TrimSpace(*p.Version)
	}
	if version == "" {
		version = packages[0].Version
	}
	for i := range packages {
		packages[i].Version = version
	}

	plans := []delivery.TiUPPlan{{
		Target:   target,
		Mirror:   mirror,
		Version:  version,
		Nightly:  delivery.IsNightlyTiUP(version),
		Packages: packages,
	}}

	_, steps, err := s.createTiUPUnit(ctx, src, "request-to-publish", digest, plans)
	if err != nil {
		return nil, publishFault(err)
	}

	ids := make([]string, 0, len(steps))
	for _, step := range steps {
		ids = append(ids, step.ID)
	}
	return ids, nil
}

// RequestToPublishSingle publishes one package from a tarball URL.
func (s *publishsrvc) RequestToPublishSingle(ctx context.Context, p *publish.TiUPPublishRequest) (string, error) {
	if p == nil || p.From == nil || p.Publish == nil {
		return "", badRequest("from and publish are required")
	}
	if p.Publish.Name == "" || p.Publish.Version == "" || p.Publish.EntryPoint == "" {
		return "", badRequest("publish.name, publish.version and publish.entry_point are required")
	}

	mirror := p.TiupMirror
	if mirror == "" {
		mirror = delivery.TiUPMirrorStaging
	}
	if _, err := tiupTargetForMirror(mirror); err != nil {
		return "", badRequest(err.Error())
	}

	pkg := delivery.TiUPPackage{
		Name:       p.Publish.Name,
		Version:    p.Publish.Version,
		OS:         p.Publish.Os,
		Arch:       p.Publish.Arch,
		EntryPoint: p.Publish.EntryPoint,
	}
	if p.Publish.Description != nil {
		pkg.Description = *p.Publish.Description
	}
	pkg.File = fmt.Sprintf("%s-%s-%s-%s.tar.gz", pkg.Name, pkg.Version, pkg.OS, pkg.Arch)

	payload := TiUPStepPayload{
		Mirror:   mirror,
		Packages: []delivery.TiUPPackage{pkg},
	}
	switch p.From.Type {
	case "http":
		if p.From.HTTP == nil || p.From.HTTP.URL == nil || *p.From.HTTP.URL == "" {
			return "", badRequest("from.http.url is required for a http source")
		}
		payload.SourceType = "http"
		payload.SourceURL = *p.From.HTTP.URL
	case "oci":
		if p.From.Oci == nil || p.From.Oci.Repo == nil || p.From.Oci.Tag == nil || p.From.Oci.File == nil {
			return "", badRequest("from.oci.repo, tag and file are required for an oci source")
		}
		payload.ArtifactURL = *p.From.Oci.Repo + ":" + *p.From.Oci.Tag
		payload.Digest = *p.From.Oci.Tag
		pkg.File = *p.From.Oci.File
		payload.Packages = []delivery.TiUPPackage{pkg}
	default:
		return "", badRequest(fmt.Sprintf("unsupported from.type %q", p.From.Type))
	}

	payloads, err := s.mirrorURLs()
	if err != nil {
		return "", publishFault(err)
	}
	payload.MirrorURL = payloads[mirror]

	unit, step, err := s.createSingleStepUnit(ctx, payload.ArtifactURL, payload.SourceURL, "request-to-publish-single", payload)
	if err != nil {
		return "", publishFault(err)
	}
	s.logger.Info().
		Str("unit_id", unit.ID).
		Str("step_id", step.ID).
		Str("mirror", mirror).
		Msg("tiup publication unit queued")
	return step.ID, nil
}

// QueryPublishingStatus maps a unit or step id onto the legacy five states.
func (s *publishsrvc) QueryPublishingStatus(ctx context.Context, p *publish.QueryPublishingStatusPayload) (publish.TiUPRequestState, error) {
	if p == nil || p.RequestID == "" {
		return "", publishFault(errors.New("request ID not found"))
	}

	unit, err := s.db.PublishUnit.Get(ctx, p.RequestID)
	if err == nil {
		return mapUnitPublishState(unit.State), nil
	}
	if !ent.IsNotFound(err) {
		return "", publishFault(err)
	}

	step, err := s.db.PublishStep.Get(ctx, p.RequestID)
	if err == nil {
		return mapStepPublishState(step.State), nil
	}
	if !ent.IsNotFound(err) {
		return "", publishFault(err)
	}

	return "", publishFault(errors.New("request ID not found"))
}

// ResetRateLimit is a no-op kept for caller compatibility.
//
// The legacy publisher kept a Redis counter per (mirror, package, os, arch) and
// skipped a nightly publish for `nightly_interval` after the previous one; this
// endpoint cleared those counters. The publish domain has no Redis and no such
// throttle: nightly convergence (accumulate, publish only the newest) is owned
// by the nightly desired-state track, which deletes this route when it lands.
// Until then a nightly build publishes on every request, serialized by the
// lane, so callers that probe this endpoint keep working.
func (s *publishsrvc) ResetRateLimit(context.Context) error {
	s.logger.Info().Msg("tiup.reset-rate-limit is a compatibility no-op; nightly convergence is owned by the nightly track")
	return nil
}

// GetPublishUnit returns a unit with its steps.

// ResumePublishUnit re-queues the failed steps of a unit.

// createTiUPUnit persists a unit and its per-target steps, seeding the
// configured dependency edges (prod before staging by default).
func (s *publishsrvc) createTiUPUnit(ctx context.Context, artifactURL, requestedBy, digest string, plans []delivery.TiUPPlan) (*ent.PublishUnit, []*ent.PublishStep, error) {
	mirrors, err := s.mirrorURLs()
	if err != nil {
		return nil, nil, err
	}

	plans = orderTiUPPlans(plans, s.publishCfg.TiUPStageDependencies())

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

	source, priority := publishSourceAndPriority(requestedBy)
	unit, err := tx.PublishUnit.Create().
		SetID(s.id()).
		SetKind(PublishKindTiUP).
		SetArtifactURL(artifactURL).
		SetDigest(digest).
		SetState(PublishStateQueued).
		SetRequestedBy(requestedBy).
		SetSource(source).
		SetPriority(priority).
		SetCreatedAt(now).
		Save(ctx)
	if err != nil {
		return nil, nil, rollback(err)
	}

	stepIDByTarget := make(map[string]string, len(plans))
	steps := make([]*ent.PublishStep, 0, len(plans))
	for i, plan := range plans {
		payload := TiUPStepPayload{
			Mirror:      plan.Mirror,
			MirrorURL:   mirrors[plan.Mirror],
			ArtifactURL: artifactURL,
			Digest:      digest,
			Nightly:     plan.Nightly,
			Packages:    plan.Packages,
		}
		raw, err := payloadMap(payload)
		if err != nil {
			return nil, nil, rollback(err)
		}

		create := tx.PublishStep.Create().
			SetID(s.id()).
			SetUnitID(unit.ID).
			SetTarget(plan.Target).
			SetPriority(priority).
			SetPayload(raw).
			SetCreatedAt(now)
		if i == 0 {
			create.SetState(PublishStepStatePending)
		} else {
			create.SetState(PublishStepStateBlocked)
			if dep := s.stepDependency(plan.Target, stepIDByTarget); dep != "" {
				create.SetDependsOn(dep)
			} else {
				create.SetState(PublishStepStatePending)
			}
		}
		step, err := create.Save(ctx)
		if err != nil {
			return nil, nil, rollback(err)
		}
		stepIDByTarget[plan.Target] = step.ID
		steps = append(steps, step)
	}

	if err := tx.Commit(); err != nil {
		return nil, nil, err
	}
	return unit, steps, nil
}

// createSingleStepUnit persists a one-step unit for the single-package path.
func (s *publishsrvc) createSingleStepUnit(ctx context.Context, artifactURL, sourceURL, requestedBy string, payload TiUPStepPayload) (*ent.PublishUnit, *ent.PublishStep, error) {
	return s.createSingleStepUnitWithID(ctx, s.id(), artifactURL, sourceURL, requestedBy, payload)
}

// createSingleStepUnitWithID is createSingleStepUnit with a caller-chosen unit
// id. The nightly reconciler reserves the id on publish_desired before the unit
// exists, so the completion callback can find the row by that id.
func (s *publishsrvc) createSingleStepUnitWithID(ctx context.Context, unitID, artifactURL, sourceURL, requestedBy string, payload TiUPStepPayload) (*ent.PublishUnit, *ent.PublishStep, error) {
	raw, err := payloadMap(payload)
	if err != nil {
		return nil, nil, err
	}
	now := s.now()

	tx, err := s.db.Tx(ctx)
	if err != nil {
		return nil, nil, err
	}
	source, priority := publishSourceAndPriority(requestedBy)
	unit, err := tx.PublishUnit.Create().
		SetID(unitID).
		SetKind(PublishKindTiUP).
		SetArtifactURL(artifactURL).
		SetState(PublishStateQueued).
		SetRequestedBy(requestedBy).
		SetSource(source).
		SetPriority(priority).
		SetCreatedAt(now).
		Save(ctx)
	if err != nil {
		_ = tx.Rollback()
		return nil, nil, err
	}
	step, err := tx.PublishStep.Create().
		SetID(s.id()).
		SetUnitID(unit.ID).
		SetTarget(tiupTargetForMirrorName(payload.Mirror)).
		SetState(PublishStepStatePending).
		SetPriority(priority).
		SetPayload(raw).
		SetCreatedAt(now).
		Save(ctx)
	if err != nil {
		_ = tx.Rollback()
		return nil, nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, nil, err
	}
	return unit, step, nil
}

// stepDependency resolves the depends_on target for a plan from the configured
// dependency map.
func (s *publishsrvc) stepDependency(target string, stepIDByTarget map[string]string) string {
	for dependency, dependent := range s.publishCfg.TiUPStageDependencies() {
		if dependent != target {
			continue
		}
		if id, ok := stepIDByTarget[dependency]; ok {
			return id
		}
	}
	return ""
}

// mirrorURLs resolves the configured mirror base URLs, using the defaults the
// legacy deployments used when the configuration omits them.
func (s *publishsrvc) mirrorURLs() (map[string]string, error) {
	out := map[string]string{
		delivery.TiUPMirrorStaging: "http://tiup.pingcap.net:8988",
		delivery.TiUPMirrorProd:    "http://tiup.pingcap.net:8987",
	}
	for mirror, url := range s.publishCfg.TiUPMirrors {
		if url != "" {
			out[mirror] = strings.TrimSuffix(url, "/")
		}
	}
	return out, nil
}

// orderTiUPPlans sorts the plans so a dependency is always created before the
// step that depends on it.
func orderTiUPPlans(plans []delivery.TiUPPlan, dependencies map[string]string) []delivery.TiUPPlan {
	ordered := make([]delivery.TiUPPlan, 0, len(plans))
	placed := make(map[string]struct{}, len(plans))
	byTarget := make(map[string]delivery.TiUPPlan, len(plans))
	for _, plan := range plans {
		byTarget[plan.Target] = plan
	}

	var place func(plan delivery.TiUPPlan)
	place = func(plan delivery.TiUPPlan) {
		if _, ok := placed[plan.Target]; ok {
			return
		}
		for dependency, dependent := range dependencies {
			if dependent == plan.Target {
				if dep, ok := byTarget[dependency]; ok {
					place(dep)
				}
			}
		}
		placed[plan.Target] = struct{}{}
		ordered = append(ordered, plan)
	}

	targets := make([]string, 0, len(byTarget))
	for target := range byTarget {
		targets = append(targets, target)
	}
	sort.Strings(targets)
	for _, target := range targets {
		place(byTarget[target])
	}
	return ordered
}

// fetchTiUPArtifactConfig reads the OCI artifact config and the manifest digest.
func fetchTiUPArtifactConfig(ctx context.Context, artifactURL string) (map[string]any, string, error) {
	ref, err := delivery.ParseImageRef(artifactURL)
	if err != nil {
		return nil, "", err
	}
	repo, err := remote.NewRepository(ref.Repository)
	if err != nil {
		return nil, "", err
	}
	repo.Client = registryAuthClient(ref.Repository)

	desc, manifestBytes, err := oras.FetchBytes(ctx, repo, ref.Tag, oras.DefaultFetchBytesOptions)
	if err != nil {
		return nil, "", fmt.Errorf("fetch manifest %s: %w", artifactURL, err)
	}
	var manifest ocispec.Manifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		return nil, "", fmt.Errorf("decode manifest: %w", err)
	}

	configBytes, err := content.FetchAll(ctx, repo, manifest.Config)
	if err != nil {
		return nil, "", fmt.Errorf("fetch config: %w", err)
	}
	var artifactConfig map[string]any
	if err := json.Unmarshal(configBytes, &artifactConfig); err != nil {
		return nil, "", fmt.Errorf("decode config: %w", err)
	}
	return artifactConfig, desc.Digest.String(), nil
}

// toTiUPUnit converts stored rows into the API type.

// mapUnitPublishState maps a unit state onto the legacy five states. A PARTIAL
// unit is reported as failed: callers must not read it as success.
func mapUnitPublishState(state string) publish.TiUPRequestState {
	switch state {
	case PublishStateQueued:
		return publish.TiUPRequestState("queued")
	case PublishStateRunning:
		return publish.TiUPRequestState("processing")
	case PublishStateSucceeded:
		return publish.TiUPRequestState("success")
	case PublishStateCanceled:
		return publish.TiUPRequestState("canceled")
	default: // partial, failed
		return publish.TiUPRequestState("failed")
	}
}

func mapStepPublishState(state string) publish.TiUPRequestState {
	switch state {
	case PublishStepStateBlocked, PublishStepStatePending:
		return publish.TiUPRequestState("queued")
	case PublishStepStateClaimed:
		return publish.TiUPRequestState("processing")
	case PublishStepStateSucceeded:
		return publish.TiUPRequestState("success")
	case PublishStepStateSkipped:
		return publish.TiUPRequestState("canceled")
	default:
		return publish.TiUPRequestState("failed")
	}
}

func tiupTargetForMirror(mirror string) (string, error) {
	switch mirror {
	case delivery.TiUPMirrorStaging:
		return delivery.TiUPTargetStaging, nil
	case delivery.TiUPMirrorProd:
		return delivery.TiUPTargetProd, nil
	default:
		return "", fmt.Errorf("unknown tiup mirror %q", mirror)
	}
}

func tiupTargetForMirrorName(mirror string) string {
	target, err := tiupTargetForMirror(mirror)
	if err != nil {
		return mirror
	}
	return target
}

func tagOf(artifactURL string) string {
	ref, err := delivery.ParseImageRef(artifactURL)
	if err != nil {
		return ""
	}
	return ref.Tag
}

func publishFault(err error) error {
	return &publish.TiUPFaultError{Name: "fault", Message: err.Error(), Fault: true}
}

// PublishKindTiUP publishes TiUP packages to the staging and prod mirrors.
const PublishKindTiUP = "tiup"
