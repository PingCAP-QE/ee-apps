package impl

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path"
	"regexp"
	"strings"
	"time"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/credentials"
	"oras.land/oras-go/v2/registry/remote/retry"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	entpublishstep "github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/publishstep"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// TiUP execution outcomes recorded on the step.
const (
	tiupResultRemoteSHA256 = "remote_sha256"
	tiupResultLocalSHA256  = "local_sha256"
	tiupResultSkipped      = "skipped_existing"
	tiupResultPackages     = "packages"
	tiupResultAction       = "action"
	tiupResultName         = "name"
	tiupResultVersion      = "version"
	tiupResultOS           = "os"
	tiupResultArch         = "arch"
	tiupResultFile         = "file"
	tiupResultURL          = "url"
)

const (
	tiupActionPublished          = "published"
	tiupActionSkippedExisting    = "skipped_existing"
	tiupActionOverwrittenNightly = "overwritten_nightly"
	// tiupActionOverwrittenNonFormal is a non-formal version (pre/beta/rc,
	// nextgen, rust, branch builds) that replaced a different remote package.
	tiupActionOverwrittenNonFormal = "overwritten_non_formal"
	// tiupActionRefusedFormalOverwrite is only logged on failure: a released
	// vX.Y.Z package must never be replaced.
	tiupActionRefusedFormalOverwrite = "refused_formal_overwrite"
	// tiupActionFailed is the action logged for every other publish failure.
	tiupActionFailed = "failed"
)

// formalTiUPVersionRegex matches the released vX.Y.Z versions. Those packages
// are immutable: replacing one would silently change what users install.
// Every other form (pre/beta/rc, nightly, nextgen, rust, branch builds) reuses
// its version string on purpose, so a new build is expected to replace it.
var formalTiUPVersionRegex = regexp.MustCompile(`^v[0-9]+\.[0-9]+\.[0-9]+$`)

// isFormalTiUPVersion reports whether a published version is a released
// vX.Y.Z version, the only form that may not be overwritten.
func isFormalTiUPVersion(version string) bool {
	return formalTiUPVersionRegex.MatchString(version)
}

// ErrLostLane means the worker no longer owns the publish lane, so it must not
// touch the mirror or write a result.
var ErrLostLane = errors.New("publish lane lost")

// ErrLaneBusy means another unit holds the lane; the step must be requeued
// instead of failed.
var ErrLaneBusy = errors.New("publish lane busy")

// TiUPStepPayload is the complete payload of a kind=tiup step. Any worker with
// the mirror credentials and the artifact reader can replay it.
type TiUPStepPayload struct {
	// Mirror is the bare mirror name: staging or prod.
	Mirror string `json:"mirror"`
	// MirrorURL is the base URL of that mirror at planning time.
	MirrorURL string `json:"mirror_url"`
	// ArtifactURL is the OCI reference the packages come from.
	ArtifactURL string `json:"artifact_url,omitempty"`
	// SourceType is "http" when the package comes from a direct URL instead of
	// an OCI artifact (the legacy request-to-publish-single path).
	SourceType string `json:"source_type,omitempty"`
	// SourceURL is the tarball URL for a http source.
	SourceURL string `json:"source_url,omitempty"`
	// Digest pins the artifact manifest, so a moved tag cannot change what is
	// published.
	Digest string `json:"digest"`
	// Nightly marks the nightly channel: its version is a constant, so a new
	// build is expected to replace the previous package of the same version.
	Nightly bool `json:"nightly,omitempty"`
	// NightlyVersion is the version this row asked for. It is recorded when a
	// desired row is published, so the completion callback records the version
	// that was actually published even if a newer push replaced desired_version
	// while the unit was running.
	NightlyVersion string                 `json:"nightly_version,omitempty"`
	Packages       []delivery.TiUPPackage `json:"packages"`
}

// tiupRunner returns the runner used by the executor.
func (s *publishsrvc) tiupRunner() tiupRunner {
	if s.tiup == nil {
		s.tiup = newTiupCLI("")
	}
	return s.tiup
}

func decodeTiUPStepPayload(stored map[string]any) (TiUPStepPayload, error) {
	var payload TiUPStepPayload
	raw, err := json.Marshal(stored)
	if err != nil {
		return payload, err
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return payload, err
	}
	return payload, nil
}

// tiupRunner is the side-effect surface of the executor, replaceable in tests.
type tiupRunner interface {
	// PublishOne publishes one package and verifies the remote hash. It is
	// idempotent: an identical remote package short-circuits, a different
	// formal vX.Y.Z is refused, and a different non-formal version is replaced.
	PublishOne(ctx context.Context, payload TiUPStepPayload, pkg delivery.TiUPPackage) (map[string]any, error)
}

// tiupCLI publishes with the tiup executable and verifies over HTTP.
type tiupCLI struct {
	bin  string
	http *http.Client
	// download fetches a file from the OCI artifact and returns its local path
	// and sha256. Tests replace it with a fixture.
	download func(ctx context.Context, artifactURL, digest, file string) (string, string, error)
}

func newTiupCLI(bin string) *tiupCLI {
	if bin == "" {
		bin = "tiup"
	}
	return &tiupCLI{
		bin:      bin,
		http:     &http.Client{Timeout: 5 * time.Minute},
		download: downloadOCIFile,
	}
}

// PublishOne implements idempotency and verification around `tiup mirror
// publish`:
//
//   - remote missing                           → publish, verify the sha256
//   - remote present, same sha                 → success without publishing
//   - remote present, other sha, formal vX.Y.Z → refuse; bump the version
//   - remote present, other sha, non-formal    → overwrite on purpose
func (t *tiupCLI) PublishOne(ctx context.Context, payload TiUPStepPayload, pkg delivery.TiUPPackage) (map[string]any, error) {
	localPath, localSHA, err := t.downloadPackage(ctx, payload, pkg.File)
	if err != nil {
		return nil, fmt.Errorf("download %s: %w", pkg.File, err)
	}
	defer os.Remove(localPath)

	remoteURL := tiupRemotePackageURL(payload.MirrorURL, pkg)

	remoteSHA, exists, err := t.remoteSHA256(ctx, remoteURL)
	if err != nil {
		return nil, err
	}
	if exists && remoteSHA == localSHA {
		return tiupPackageResult(pkg, remoteURL, localSHA, remoteSHA, tiupActionSkippedExisting, true), nil
	}
	// A released version must never be overwritten: that would silently change
	// what users install. Every non-formal channel (nightly, pre/beta/rc,
	// nextgen, rust, branch builds) reuses its version string on purpose, so a
	// new build replaces the previous package.
	if exists && isFormalTiUPVersion(pkg.Version) {
		return nil, &tiupOverwriteRefusedError{pkg: pkg, url: remoteURL, remoteSHA: remoteSHA, localSHA: localSHA}
	}
	// `exists` is reused by the post-check below; keep what happened before.
	overwritten := exists

	if err := t.publish(ctx, payload.MirrorURL, localPath, pkg); err != nil {
		return nil, err
	}

	published, exists, err := t.remoteSHA256(ctx, remoteURL)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, fmt.Errorf("post check failed: %s is missing after publish", remoteURL)
	}
	if published != localSHA {
		return nil, fmt.Errorf("post check failed: %s sha256 %s != %s", remoteURL, published, localSHA)
	}

	action := tiupActionPublished
	if overwritten {
		action = tiupActionOverwrittenNonFormal
		if payload.Nightly {
			action = tiupActionOverwrittenNightly
		}
	}
	return tiupPackageResult(pkg, remoteURL, localSHA, published, action, false), nil
}

// tiupOverwriteRefusedError is returned when a released vX.Y.Z package differs
// from the remote one. It carries the package coordinates so the executor logs
// the refusal with the same fields as a successful action.
type tiupOverwriteRefusedError struct {
	pkg       delivery.TiUPPackage
	url       string
	remoteSHA string
	localSHA  string
}

func (e *tiupOverwriteRefusedError) Error() string {
	return fmt.Sprintf("refusing to overwrite formal version %s (%s): remote sha256 %s differs from %s",
		e.pkg.Version, e.url, e.remoteSHA, e.localSHA)
}

func tiupPackageResult(pkg delivery.TiUPPackage, url, localSHA, remoteSHA, action string, skipped bool) map[string]any {
	return map[string]any{
		tiupResultSkipped:      skipped,
		tiupResultRemoteSHA256: remoteSHA,
		tiupResultLocalSHA256:  localSHA,
		tiupResultAction:       action,
		tiupResultName:         pkg.Name,
		tiupResultVersion:      pkg.Version,
		tiupResultOS:           pkg.OS,
		tiupResultArch:         pkg.Arch,
		tiupResultFile:         pkg.File,
		tiupResultURL:          url,
	}
}

// downloadPackage resolves the local tarball for a package, from a direct URL
// or from the OCI artifact.
func (t *tiupCLI) downloadPackage(ctx context.Context, payload TiUPStepPayload, file string) (string, string, error) {
	if payload.SourceType == "http" {
		return t.downloadHTTP(ctx, payload.SourceURL)
	}
	return t.download(ctx, payload.ArtifactURL, payload.Digest, file)
}

// downloadHTTP fetches a tarball over HTTP into a temp file.
func (t *tiupCLI) downloadHTTP(ctx context.Context, url string) (string, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", "", err
	}
	resp, err := t.http.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("download %s: status %d", url, resp.StatusCode)
	}

	tmp, err := os.CreateTemp("", "tiup-publish-*")
	if err != nil {
		return "", "", err
	}
	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(tmp, hash), resp.Body); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return "", "", err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return "", "", err
	}
	return tmp.Name(), hex.EncodeToString(hash.Sum(nil)), nil
}

// publish runs `tiup mirror publish` with a context, so a hung command is
// killed at the step deadline instead of holding the lane.
//
// `--standalone` is intentionally never passed: the pre-migration publisher
// dropped the artifact's standalone flag and this port preserves that.

func (t *tiupCLI) publish(ctx context.Context, mirrorURL, localPath string, pkg delivery.TiUPPackage) error {
	args := []string{
		"mirror", "publish", pkg.Name, pkg.Version, localPath, pkg.EntryPoint,
		"--os", pkg.OS,
		"--arch", pkg.Arch,
		"--desc", pkg.Description,
	}
	cmd := exec.CommandContext(ctx, t.bin, args...)
	cmd.Env = append(os.Environ(), "TIUP_MIRRORS="+mirrorURL)
	// A killed tiup (or a child holding its stdout) must not block the worker
	// past the step deadline.
	cmd.WaitDelay = 2 * time.Second

	out, err := cmd.CombinedOutput()
	if err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("tiup mirror publish timed out: %w", ctx.Err())
		}
		return fmt.Errorf("tiup mirror publish failed: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// remoteSHA256 returns the sha256 of the remote package and whether it exists.
func (t *tiupCLI) remoteSHA256(ctx context.Context, url string) (string, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", false, err
	}
	resp, err := t.http.Do(req)
	if err != nil {
		return "", false, fmt.Errorf("check %s: %w", url, err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusNotFound:
		return "", false, nil
	case http.StatusOK:
	default:
		return "", false, fmt.Errorf("check %s: unexpected status %d", url, resp.StatusCode)
	}

	hash := sha256.New()
	if _, err := io.Copy(hash, resp.Body); err != nil {
		return "", false, fmt.Errorf("read %s: %w", url, err)
	}
	return hex.EncodeToString(hash.Sum(nil)), true, nil
}

// tiupRemotePackageURL is where `tiup mirror publish` places a package.
func tiupRemotePackageURL(mirrorURL string, pkg delivery.TiUPPackage) string {
	return strings.TrimSuffix(mirrorURL, "/") + "/" + path.Base(fmt.Sprintf("%s-%s-%s-%s.tar.gz",
		pkg.Name, pkg.Version, pkg.OS, pkg.Arch))
}

// downloadOCIFile fetches one file of an OCI artifact, using the mounted docker
// credentials when available (the tidbx repository is not anonymously
// readable) and anonymous access otherwise.
func downloadOCIFile(ctx context.Context, artifactURL, digest, file string) (string, string, error) {
	ref, err := delivery.ParseImageRef(artifactURL)
	if err != nil {
		return "", "", err
	}
	repo, err := remote.NewRepository(ref.Repository)
	if err != nil {
		return "", "", err
	}
	repo.Client = registryAuthClient(ref.Repository)

	source := digest
	if source == "" {
		source = ref.Tag
	}

	_, manifestBytes, err := oras.FetchBytes(ctx, repo, source, oras.DefaultFetchBytesOptions)
	if err != nil {
		return "", "", fmt.Errorf("fetch manifest %s: %w", source, err)
	}
	var manifest ocispec.Manifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		return "", "", fmt.Errorf("decode manifest: %w", err)
	}

	var layer *ocispec.Descriptor
	for i := range manifest.Layers {
		if manifest.Layers[i].Annotations[ocispec.AnnotationTitle] == file {
			layer = &manifest.Layers[i]
			break
		}
	}
	if layer == nil {
		return "", "", fmt.Errorf("artifact %s has no file %q", artifactURL, file)
	}

	rc, err := content.FetchAll(ctx, repo, *layer)
	if err != nil {
		return "", "", fmt.Errorf("fetch %s: %w", file, err)
	}

	tmp, err := os.CreateTemp("", "tiup-publish-*")
	if err != nil {
		return "", "", err
	}
	if _, err := tmp.Write(rc); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return "", "", err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return "", "", err
	}

	sum := sha256.Sum256(rc)
	return tmp.Name(), hex.EncodeToString(sum[:]), nil
}

// registryAuthClient resolves credentials from the process docker config, then
// falls back to anonymous pull.
func registryAuthClient(repository string) *auth.Client {
	registry := strings.SplitN(repository, "/", 2)[0]

	credential := auth.EmptyCredential
	if store, err := credentials.NewStoreFromDocker(credentials.StoreOptions{}); err == nil {
		if cred, err := store.Get(context.Background(), registry); err == nil {
			credential = cred
		}
	}

	return &auth.Client{
		Client:     retry.DefaultClient,
		Cache:      auth.DefaultCache,
		Credential: auth.StaticCredential(registry, credential),
	}
}

// executeTiUPStep runs one claimed step under the lane.
//
// The lane is acquired before anything irreversible happens, the execution has
// a hard timeout, a heartbeat renews the lease, and the lane is validated
// before and after the publish. A lost lane yields ErrLostLane and the result
// is not written back.
// runTiUPStep executes one claimed TiUP step and records its outcome through
// the same fenced helpers the image path uses.
func (s *publishsrvc) runTiUPStep(ctx context.Context, step *ent.PublishStep) {
	s.inflight.Add(1)
	defer s.inflight.Done()

	log := s.logger.With().
		Str("step_id", step.ID).
		Str("unit_id", step.UnitID).
		Str("target", step.Target).
		Logger()

	result, err := s.executeTiUPStep(ctx, step, step.ClaimedBy)
	switch {
	case errors.Is(err, ErrLaneBusy):
		// Another unit legitimately owns the mirror; the step waits its turn.
		if s.metrics != nil {
			s.metrics.recordLaneBusy(step.Target)
		}
		if ok, deferErr := s.deferStep(ctx, step); deferErr != nil {
			log.Err(deferErr).Msg("failed to requeue the step for a busy lane")
		} else if ok {
			log.Warn().
				Int("attempts", step.Attempts).
				Dur("waited", s.now().Sub(step.CreatedAt)).
				Msg("publish lane busy, step requeued")
		}
		return
	case errors.Is(err, ErrLostLane):
		// The lease was reclaimed: discard the result, the new owner decides.
		log.Warn().Msg("publish lane lost, discarding the step result")
		return
	case err != nil:
		s.recordStepFailure(ctx, step, err, &log)
		return
	}

	stored, err := payloadMap(result)
	if err != nil {
		log.Err(err).Msg("failed to encode the step result")
		return
	}
	ok, err := s.markStepSucceeded(ctx, step, stored)
	if err != nil {
		log.Err(err).Msg("failed to record the step result")
		return
	}
	if !ok {
		log.Warn().Msg("step lease lost, success not recorded")
		return
	}
	s.refreshUnit(ctx, step.UnitID)

	packages, _ := result[tiupResultPackages].([]map[string]any)
	published, skipped, overwritten := 0, 0, 0
	for _, pkg := range packages {
		switch pkg[tiupResultAction] {
		case tiupActionPublished:
			published++
		case tiupActionSkippedExisting:
			skipped++
		case tiupActionOverwrittenNightly, tiupActionOverwrittenNonFormal:
			overwritten++
		}
	}
	log.Info().
		Int("packages", len(packages)).
		Int("published", published).
		Int("skipped", skipped).
		Int("overwritten", overwritten).
		Msg("tiup step finished")
}

// deferStep puts a claimed step back into the queue with a backoff, fenced on
// the claim token.
func (s *publishsrvc) deferStep(ctx context.Context, step *ent.PublishStep) (bool, error) {
	updated, err := s.db.PublishStep.Update().
		Where(
			entpublishstep.ID(step.ID),
			entpublishstep.State(PublishStepStateClaimed),
			entpublishstep.ClaimedBy(step.ClaimedBy),
			entpublishstep.AttemptsGT(0),
		).
		SetState(PublishStepStatePending).
		SetAvailableAt(s.now().Add(s.laneBusyBackoff(step.Attempts))).
		ClearClaimedBy().
		ClearLeaseUntil().
		// The claim incremented attempts before the lane was known to be busy.
		// Waiting for the lane is not an execution attempt, so undo it.
		AddAttempts(-1).
		Save(ctx)
	return updated == 1, err
}

func (s *publishsrvc) executeTiUPStep(ctx context.Context, step *ent.PublishStep, workerID string) (map[string]any, error) {
	payload, err := decodeTiUPStepPayload(step.Payload)
	if err != nil {
		return nil, err
	}
	if payload.MirrorURL == "" {
		return nil, fmt.Errorf("step %s has no mirror url in its payload", step.ID)
	}

	acquired, err := s.acquireLane(ctx, step.UnitID, workerID)
	if err != nil {
		return nil, err
	}
	if !acquired {
		return nil, ErrLaneBusy
	}

	runCtx := ctx
	if hard := s.workerCfg.HardTimeoutDuration(); hard > 0 {
		var cancel context.CancelFunc
		runCtx, cancel = context.WithTimeout(ctx, hard)
		defer cancel()
	}
	runCtx, cancelRun := context.WithCancel(runCtx)
	defer cancelRun()

	go s.lane.heartbeat(runCtx, cancelRun, step.UnitID, workerID,
		s.workerCfg.HeartbeatDuration(), s.laneLease())

	if ok, err := s.validateLane(runCtx, step.UnitID, workerID); err != nil {
		return nil, err
	} else if !ok {
		return nil, ErrLostLane
	}

	log := s.logger.With().
		Str("unit_id", step.UnitID).
		Str("step_id", step.ID).
		Str("target", step.Target).
		Str("mirror", payload.Mirror).
		Logger()

	runner := s.tiupRunner()
	results := make([]map[string]any, 0, len(payload.Packages))
	allSkipped := true
	for _, pkg := range payload.Packages {
		if err := runCtx.Err(); err != nil {
			return nil, err
		}
		started := time.Now()
		if pkg.Version == "" {
			log.Warn().
				Str("pkg", pkg.Name).
				Str("file", pkg.File).
				Msg("tiup package has no version; treating it as non-formal (overwrite allowed)")
		}
		result, err := runner.PublishOne(runCtx, payload, pkg)
		if err != nil {
			failure := log.Error().Err(err).
				Str("pkg", pkg.Name).
				Str("version", pkg.Version).
				Str("os", pkg.OS).
				Str("arch", pkg.Arch).
				Str("file", pkg.File).
				Bool("formal_version", isFormalTiUPVersion(pkg.Version)).
				Dur("duration", time.Since(started))
			var refused *tiupOverwriteRefusedError
			if errors.As(err, &refused) {
				failure = failure.
					Str("action", tiupActionRefusedFormalOverwrite).
					Str("remote_sha256", refused.remoteSHA).
					Str("local_sha256", refused.localSHA).
					Str("url", refused.url)
			} else {
				failure = failure.Str("action", tiupActionFailed)
			}
			failure.Msg("tiup package failed")
			return nil, err
		}
		if result[tiupResultSkipped] != true {
			allSkipped = false
		}
		log.Info().
			Str("pkg", pkg.Name).
			Str("version", pkg.Version).
			Str("os", pkg.OS).
			Str("arch", pkg.Arch).
			Str("file", pkg.File).
			Str("action", fmt.Sprint(result[tiupResultAction])).
			Bool("skipped", result[tiupResultSkipped] == true).
			Str("remote_sha256", fmt.Sprint(result[tiupResultRemoteSHA256])).
			Str("url", fmt.Sprint(result[tiupResultURL])).
			Dur("duration", time.Since(started)).
			Msg("tiup package processed")
		results = append(results, result)
	}

	if ok, err := s.validateLane(ctx, step.UnitID, workerID); err != nil {
		return nil, err
	} else if !ok {
		return nil, ErrLostLane
	}

	return map[string]any{
		"mirror":           payload.Mirror,
		tiupResultSkipped:  allSkipped,
		tiupResultPackages: results,
	}, nil
}
