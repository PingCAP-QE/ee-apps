package impl

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/publish"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

const (
	publishTestDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	publishTestRules  = `
deprecated:
  - example.com/hub/pingcap/legacy/image
image_copy_rules:
  example.com/hub/pingcap/widget/image:
    - description: trunk
      tags_regex:
        - ^(master|main)$
      constant_tags:
        - nightly
      dest_repositories:
        - docker.io/pingcap/widget
    - description: ga
      tags_regex:
        - ^v[0-9]+[.][0-9]+[.][0-9]+$
      dest_repositories:
        - docker.io/pingcap/widget
        - gcr.io/pingcap-public/widget
    - description: enterprise
      tags_regex:
        - ^(v[0-9]+[.][0-9]+[.][0-9]+)-enterprise$
      tag_regex_replace: "$1"
      dest_repositories:
        - gcr.io/pingcap-public/widget
  example.com/hub/pingcap/legacy/image:
    - description: legacy
      tags_regex:
        - ^v.*$
      dest_repositories:
        - docker.io/pingcap/legacy
`
)

// fakeImageOps records registry calls and answers from a table.
type fakeImageOps struct {
	digests   map[string]string
	resolved  []string
	digestErr error

	copies  []string
	copyErr error
}

func (f *fakeImageOps) Digest(_ context.Context, ref string) (string, error) {
	f.resolved = append(f.resolved, ref)
	if f.digestErr != nil {
		return "", f.digestErr
	}
	digest, ok := f.digests[ref]
	if !ok {
		return "", fmt.Errorf("%s: not found", ref)
	}
	return digest, nil
}

func (f *fakeImageOps) Copy(_ context.Context, src, dst string) error {
	f.copies = append(f.copies, src+" -> "+dst)
	return f.copyErr
}

func newPublishTestService(t *testing.T, rules string, ops *fakeImageOps) *publishsrvc {
	t.Helper()

	path := filepath.Join(t.TempDir(), "delivery.yaml")
	require.NoError(t, os.WriteFile(path, []byte(rules), 0o644))

	logger := zerolog.Nop()
	cfg := &config.Service{Publish: config.Publish{DeliveryConfigPath: path}}
	svc := newPublishService(&logger, serverlessTestDB(t), cfg)
	if ops != nil {
		svc.images = ops
		svc.planner.resolver = ops
	}
	return svc
}

func TestPublishImagesPlansOneStepPerDestination(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	require.NoError(t, err)

	require.NotEmpty(t, unit.ID)
	require.Equal(t, PublishKindImageCopy, unit.Kind)
	require.Equal(t, publish.PublishState(PublishStateQueued), unit.State)
	require.Equal(t, publishTestDigest, *unit.Digest)
	require.Equal(t, []string{"example.com/hub/pingcap/widget/image:v8.5.0"}, ops.resolved,
		"the digest is resolved once per request")

	// One step per destination, and the derived tag is part of the payload.
	plans := map[string][]string{}
	for _, step := range unit.Steps {
		require.Equal(t, publish.PublishStepState(PublishStepStatePending), step.State)
		payload, ok := step.Payload.(map[string]any)
		require.True(t, ok)
		require.Equal(t, "example.com/hub/pingcap/widget/image@"+publishTestDigest, payload["source"])
		plans[destOf(step)] = toStringSlice(payload["tags"])
	}
	require.Equal(t, map[string][]string{
		"docker.io/pingcap/widget":     {"v8.5.0"},
		"gcr.io/pingcap-public/widget": {"v8.5.0"},
	}, plans)

	// Persisted, not just returned.
	stored, err := svc.GetUnit(context.Background(), &publish.GetUnitPayload{ID: unit.ID})
	require.NoError(t, err)
	require.Len(t, stored.Steps, 2)
}

func TestPublishImagesDerivesTheEnterpriseTag(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0-enterprise": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0-enterprise"),
	})
	require.NoError(t, err)
	require.Len(t, unit.Steps, 1)
	payload := unit.Steps[0].Payload.(map[string]any)
	require.Equal(t, []string{"v8.5.0-enterprise", "v8.5.0"}, toStringSlice(payload["tags"]))
}

func TestPublishImagesDedupesDestinationsAndMergesTags(t *testing.T) {
	rules := `
image_copy_rules:
  example.com/hub/pingcap/widget/image:
    - description: first
      tags_regex:
        - ^v[0-9]+[.][0-9]+[.][0-9]+$
      dest_repositories:
        - docker.io/pingcap/widget
    - description: second
      tags_regex:
        - ^v[0-9]+[.][0-9]+[.][0-9]+$
      constant_tags:
        - latest
      dest_repositories:
        - docker.io/pingcap/widget
        - gcr.io/pingcap-public/widget
`
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, rules, ops)

	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	require.NoError(t, err)
	require.Len(t, unit.Steps, 2, "the duplicate destination collapses into one step")

	for _, step := range unit.Steps {
		if destOf(step) != "docker.io/pingcap/widget" {
			continue
		}
		payload := step.Payload.(map[string]any)
		require.Equal(t, []string{"v8.5.0", "latest"}, toStringSlice(payload["tags"]))
	}
}

func TestPublishImagesPinsTheDigestAtPlanningTime(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		Image: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	require.NoError(t, err)

	// The source tag moves to a new digest after planning.
	moved := "sha256:2222222222222222222222222222222222222222222222222222222222222222"
	ops.digests["example.com/hub/pingcap/widget/image:v8.5.0"] = moved

	stored, err := svc.GetUnit(context.Background(), &publish.GetUnitPayload{ID: unit.ID})
	require.NoError(t, err)
	for _, step := range stored.Steps {
		payload := step.Payload.(map[string]any)
		require.Equal(t, publishTestDigest, payload["digest"])
		require.Equal(t, "example.com/hub/pingcap/widget/image@"+publishTestDigest, payload["source"])
	}
}

func TestPublishImagesWithoutMatchingRuleIsANoop(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{}}
	svc := newPublishTestService(t, publishTestRules, ops)

	for _, src := range []string{
		"example.com/hub/pingcap/unknown/image:v8.5.0",
		"example.com/hub/pingcap/widget/image:not-a-version",
		"example.com/hub/pingcap/legacy/image:v8.5.0", // deprecated
	} {
		unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
			ArtifactURL: stringPtr(src),
		})
		require.NoError(t, err, src)
		require.Empty(t, unit.ID, src)
		require.Empty(t, unit.Steps, src)
		require.Equal(t, publish.PublishState(PublishStateSucceeded), unit.State, src)
	}

	require.Empty(t, ops.resolved, "nothing to deliver means no registry call")
	units, err := svc.db.PublishUnit.Query().All(context.Background())
	require.NoError(t, err)
	require.Empty(t, units, "a no-op request must not create a unit")
}

func TestPublishImagesRejectsUnusableReferences(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{}}
	svc := newPublishTestService(t, publishTestRules, ops)

	tests := map[string]*publish.PublishImagesPayload{
		"empty":       {},
		"no tag":      {ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image")},
		"digest only": {ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image@" + publishTestDigest)},
	}
	for name, payload := range tests {
		_, err := svc.PublishImages(context.Background(), payload)
		var httpErr *publish.HTTPError
		require.ErrorAs(t, err, &httpErr, name)
		require.Equal(t, http.StatusBadRequest, httpErr.Code, name)
	}
}

func TestPublishImagesReportsRegistryFailuresAsBadGateway(t *testing.T) {
	ops := &fakeImageOps{digestErr: errors.New("dial tcp: i/o timeout")}
	svc := newPublishTestService(t, publishTestRules, ops)

	_, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	var httpErr *publish.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusBadGateway, httpErr.Code)
}

func TestPublishImagesUsesThePinnedDigestFromTheReference(t *testing.T) {
	pinned := "sha256:3333333333333333333333333333333333333333333333333333333333333333"
	ops := &fakeImageOps{digests: map[string]string{}}
	svc := newPublishTestService(t, publishTestRules, ops)

	unit, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0@" + pinned),
	})
	require.NoError(t, err)
	require.Equal(t, pinned, *unit.Digest)
	require.Empty(t, ops.resolved, "a pinned reference does not need a lookup")
}

func TestListUnitsFiltersAndOrdersNewestFirst(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
		"example.com/hub/pingcap/widget/image:v8.4.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)

	// Deterministic clock so the ordering assertion is stable.
	tick := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	svc.nowFn = func() time.Time {
		tick = tick.Add(time.Second)
		return tick
	}

	first, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.4.0"),
	})
	require.NoError(t, err)
	second, err := svc.PublishImages(context.Background(), &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	require.NoError(t, err)

	units, err := svc.ListUnits(context.Background(), &publish.ListUnitsPayload{Limit: 10})
	require.NoError(t, err)
	require.Len(t, units, 2)
	require.Equal(t, second.ID, units[0].ID)
	require.Equal(t, first.ID, units[1].ID)
	require.Len(t, units[0].Steps, 2, "the list includes the steps of every unit")

	filtered, err := svc.ListUnits(context.Background(), &publish.ListUnitsPayload{
		Status: stringPtr(PublishStateRunning),
		Limit:  10,
	})
	require.NoError(t, err)
	require.Empty(t, filtered)
}

func TestRetryUnitOnlyRequeuesFailedSteps(t *testing.T) {
	ops := &fakeImageOps{digests: map[string]string{
		"example.com/hub/pingcap/widget/image:v8.5.0": publishTestDigest,
	}}
	svc := newPublishTestService(t, publishTestRules, ops)
	ctx := context.Background()

	unit, err := svc.PublishImages(ctx, &publish.PublishImagesPayload{
		ArtifactURL: stringPtr("example.com/hub/pingcap/widget/image:v8.5.0"),
	})
	require.NoError(t, err)
	require.Len(t, unit.Steps, 2)

	okStepID, failedStepID := unit.Steps[0].ID, unit.Steps[1].ID
	finish := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	require.NoError(t, svc.db.PublishStep.UpdateOneID(okStepID).
		SetState(PublishStepStateSucceeded).
		SetAttempts(1).
		SetFinishedAt(finish).
		Exec(ctx))
	require.NoError(t, svc.db.PublishStep.UpdateOneID(failedStepID).
		SetState(PublishStepStateFailed).
		SetAttempts(3).
		SetLastError("boom").
		SetFinishedAt(finish).
		Exec(ctx))
	require.NoError(t, svc.db.PublishUnit.UpdateOneID(unit.ID).
		SetState(PublishStatePartial).
		SetFinishedAt(finish).
		Exec(ctx))

	retried, err := svc.RetryUnit(ctx, &publish.RetryUnitPayload{ID: unit.ID})
	require.NoError(t, err)
	require.Equal(t, publish.PublishState(PublishStateQueued), retried.State)
	require.Nil(t, retried.FinishedAt)

	okStep, err := svc.db.PublishStep.Get(ctx, okStepID)
	require.NoError(t, err)
	require.Equal(t, PublishStepStateSucceeded, okStep.State)
	require.Equal(t, 1, okStep.Attempts)

	retriedStep, err := svc.db.PublishStep.Get(ctx, failedStepID)
	require.NoError(t, err)
	require.Equal(t, PublishStepStatePending, retriedStep.State)
	require.Equal(t, 0, retriedStep.Attempts)
	require.Empty(t, retriedStep.LastError)
	require.Nil(t, retriedStep.FinishedAt)

	// Nothing is left to retry once every step succeeded.
	require.NoError(t, svc.db.PublishStep.Update().SetState(PublishStepStateSucceeded).Exec(ctx))
	_, err = svc.RetryUnit(ctx, &publish.RetryUnitPayload{ID: unit.ID})
	var httpErr *publish.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusBadRequest, httpErr.Code)
}

func TestGetUnitNotFound(t *testing.T) {
	svc := newPublishTestService(t, publishTestRules, &fakeImageOps{})
	_, err := svc.GetUnit(context.Background(), &publish.GetUnitPayload{ID: "missing"})
	var httpErr *publish.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusNotFound, httpErr.Code)
}

// destOf returns the destination repository of an API step.
func destOf(step *publish.PublishStep) string {
	if step.Dest == nil {
		return ""
	}
	return *step.Dest
}

func toStringSlice(value any) []string {
	raw, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		out = append(out, item.(string))
	}
	return out
}
