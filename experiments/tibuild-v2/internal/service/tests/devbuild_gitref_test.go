package impl_test

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

func gitRefCreatePayload(creator, ref string, dryrun bool) *devbuild.CreatePayload {
	return &devbuild.CreatePayload{
		CreatedBy: stringPtr(creator),
		Request: &devbuild.DevBuildSpec{
			Product:  "pd",
			Edition:  "community",
			GitRef:   ref,
			Platform: "linux/amd64",
		},
		Dryrun: dryrun,
	}
}

func countBuildsByCreator(t *testing.T, env *testEnv, creator string) int {
	t.Helper()
	builds, err := env.service.List(context.Background(), &devbuild.ListPayload{
		Page:      1,
		PageSize:  100,
		Hotfix:    false,
		CreatedBy: &creator,
	})
	require.NoError(t, err)
	return len(builds)
}

func TestCreateRejectsMissingGitRef(t *testing.T) {
	commitSHA := strings.Repeat("a", 40)

	tests := []struct {
		name       string
		ref        string
		wantSubstr string
	}{
		{name: "branch", ref: "branch/xxxx", wantSubstr: `branch "xxxx" not found in pingcap/pd`},
		{name: "tag", ref: "tag/v0.0.0-missing", wantSubstr: `tag "v0.0.0-missing" not found in pingcap/pd`},
		{name: "pull", ref: "pull/999999", wantSubstr: `pull request #999999 not found in pingcap/pd`},
		{name: "commit", ref: "commit/" + commitSHA, wantSubstr: `commit "` + commitSHA + `" not found in pingcap/pd`},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := setupTestEnv(t)
			defer teardownTestEnv(env)

			env.github.markMissing("pingcap/pd", tt.ref)
			env.github.reset()
			env.cloudEvents.reset()
			creator := "missing-" + tt.name + "@test"

			_, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, tt.ref, false))

			var badRequest *devbuild.DevBuildBadRequestError
			require.ErrorAs(t, err, &badRequest)
			assert.Equal(t, 400, badRequest.Code)
			assert.Contains(t, badRequest.Message, tt.wantSubstr)

			assert.Equal(t, 0, countBuildsByCreator(t, env, creator), "no DevBuild row must be persisted")
			assert.Equal(t, 0, env.cloudEvents.count(), "no CloudEvent must be sent")
			assert.Equal(t, 1, env.github.requestCount(), "the ref must be checked exactly once")
		})
	}
}

func TestCreateFailsOnGitHubUpstreamError(t *testing.T) {
	for _, status := range []int{401, 500, 503} {
		t.Run("status-"+strconv.Itoa(status), func(t *testing.T) {
			env := setupTestEnv(t)
			defer teardownTestEnv(env)

			env.github.setFailStatus(status)
			env.github.reset()
			env.cloudEvents.reset()
			creator := "upstream-" + strconv.Itoa(status) + "@test"

			_, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, "branch/master", false))

			var internal *devbuild.DevBuildInternalServerError
			require.ErrorAs(t, err, &internal)
			assert.Equal(t, 500, internal.Code)
			assert.Contains(t, internal.Message, "pingcap/pd")

			assert.Equal(t, 0, countBuildsByCreator(t, env, creator))
			assert.Equal(t, 0, env.cloudEvents.count())
			assert.Equal(t, 1, env.github.requestCount())
		})
	}
}

func TestCreateResolvesGitRefOnce(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	env.github.reset()
	env.cloudEvents.reset()
	creator := "valid-branch@test"
	expectedSHA := fakeSHA("pingcap/pd|branch/master")

	build, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, "branch/master", false))
	require.NoError(t, err)

	require.NotNil(t, build.Spec.GitHash)
	assert.Equal(t, expectedSHA, *build.Spec.GitHash)
	assert.Equal(t, 1, env.github.requestCount(), "create must resolve the ref exactly once")
	require.Equal(t, 1, env.cloudEvents.count())

	var event struct {
		Ref   string `json:"ref"`
		After string `json:"after"`
	}
	require.NoError(t, json.Unmarshal(env.cloudEvents.body(), &event))
	assert.Equal(t, "refs/heads/master", event.Ref)
	assert.Equal(t, expectedSHA, event.After)
}

func TestCreateDryrunValidatesGitRef(t *testing.T) {
	t.Run("missing ref", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		env.github.markMissing("pingcap/pd", "branch/xxxx")
		env.github.reset()
		env.cloudEvents.reset()
		creator := "dryrun-missing@test"

		_, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, "branch/xxxx", true))

		var badRequest *devbuild.DevBuildBadRequestError
		require.ErrorAs(t, err, &badRequest)
		assert.Equal(t, 0, countBuildsByCreator(t, env, creator))
		assert.Equal(t, 0, env.cloudEvents.count())
	})

	t.Run("valid ref", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		env.github.reset()
		env.cloudEvents.reset()
		creator := "dryrun-valid@test"

		build, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, "branch/master", true))
		require.NoError(t, err)
		require.NotNil(t, build.Spec.GitHash)
		assert.Equal(t, fakeSHA("pingcap/pd|branch/master"), *build.Spec.GitHash)
		assert.Equal(t, 1, env.github.requestCount())
		assert.Equal(t, 0, env.cloudEvents.count())
	})
}

func TestRerunReusesStoredGitHash(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()
	creator := "rerun@test"
	expectedSHA := fakeSHA("pingcap/pd|branch/master")

	created, err := env.service.Create(ctx, gitRefCreatePayload(creator, "branch/master", false))
	require.NoError(t, err)
	require.NotNil(t, created.Spec.GitHash)
	require.Equal(t, expectedSHA, *created.Spec.GitHash)

	terminal := devbuild.BuildStatus("SUCCESS")
	_, err = env.service.Update(ctx, &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: terminal},
	})
	require.NoError(t, err)

	// Make GitHub unavailable: rerun must not need it when the SHA is stored.
	env.github.setFailStatus(500)
	env.github.reset()
	env.cloudEvents.reset()

	rerun, err := env.service.Rerun(ctx, &devbuild.RerunPayload{ID: created.ID})
	require.NoError(t, err)
	require.NotNil(t, rerun.Spec.GitHash)
	assert.Equal(t, expectedSHA, *rerun.Spec.GitHash)
	assert.Equal(t, 0, env.github.requestCount(), "rerun must reuse the stored SHA")
	assert.Equal(t, 1, env.cloudEvents.count())
}

func TestRerunResolvesLegacyEmptyGitHash(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()
	creator := "legacy@test"

	db, err := ent.Open("sqlite3", env.cfg.Store.DSN)
	require.NoError(t, err)
	defer db.Close()

	legacy, err := db.DevBuild.Create().
		SetProduct("pd").
		SetEdition("community").
		SetGithubRepo("pingcap/pd").
		SetGitRef("branch/nonexistent").
		SetGitHash("").
		SetStatus("SUCCESS").
		SetCreatedBy(creator).
		SetPlatform("linux/amd64").
		SetCreatedAt(time.Now()).
		Save(ctx)
	require.NoError(t, err)

	env.github.markMissing("pingcap/pd", "branch/nonexistent")
	env.github.reset()
	env.cloudEvents.reset()

	_, err = env.service.Rerun(ctx, &devbuild.RerunPayload{ID: legacy.ID})

	var badRequest *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &badRequest)
	assert.Contains(t, badRequest.Message, "branch \"nonexistent\" not found")
	assert.Equal(t, 1, countBuildsByCreator(t, env, creator), "no rerun record must be created")
	assert.Equal(t, 0, env.cloudEvents.count())
	assert.Equal(t, 1, env.github.requestCount())
}

func TestRerunRejectsNonTerminalBuild(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()
	creator := "rerun-pending@test"

	created, err := env.service.Create(ctx, gitRefCreatePayload(creator, "branch/master", false))
	require.NoError(t, err)

	_, err = env.service.Rerun(ctx, &devbuild.RerunPayload{ID: created.ID})

	var badRequest *devbuild.DevBuildBadRequestError
	require.ErrorAs(t, err, &badRequest)
	assert.Contains(t, badRequest.Message, "only terminal builds")
	assert.Equal(t, 1, countBuildsByCreator(t, env, creator))
}

func TestRerunLegacyRefUpstreamFailure(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()
	creator := "legacy-upstream@test"

	db, err := ent.Open("sqlite3", env.cfg.Store.DSN)
	require.NoError(t, err)
	defer db.Close()

	legacy, err := db.DevBuild.Create().
		SetProduct("pd").
		SetEdition("community").
		SetGithubRepo("pingcap/pd").
		SetGitRef("branch/master").
		SetGitHash("").
		SetStatus("SUCCESS").
		SetCreatedBy(creator).
		SetPlatform("linux/amd64").
		SetCreatedAt(time.Now()).
		Save(ctx)
	require.NoError(t, err)

	env.github.setFailStatus(500)
	env.github.reset()
	env.cloudEvents.reset()

	_, err = env.service.Rerun(ctx, &devbuild.RerunPayload{ID: legacy.ID})

	var internal *devbuild.DevBuildInternalServerError
	require.ErrorAs(t, err, &internal)
	assert.Equal(t, 500, internal.Code)
	assert.Contains(t, internal.Message, "pingcap/pd")
	assert.Equal(t, 1, countBuildsByCreator(t, env, creator))
	assert.Equal(t, 0, env.cloudEvents.count())
}

func TestRerunNotFound(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	_, err := env.service.Rerun(context.Background(), &devbuild.RerunPayload{ID: 999999})

	var notFound *devbuild.DevBuildNotFoundError
	require.ErrorAs(t, err, &notFound)
}

func TestRerunReportsTriggerFailure(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()
	creator := "rerun-trigger-failure@test"

	created, err := env.service.Create(ctx, gitRefCreatePayload(creator, "branch/master", false))
	require.NoError(t, err)

	terminal := devbuild.BuildStatus("SUCCESS")
	_, err = env.service.Update(ctx, &devbuild.UpdatePayload{
		ID:     created.ID,
		Status: &devbuild.DevBuildStatus{Status: terminal},
	})
	require.NoError(t, err)

	// A closed listener forces the CloudEvent delivery to fail.
	env.cloudEventServer.Close()

	_, err = env.service.Rerun(ctx, &devbuild.RerunPayload{ID: created.ID})

	var internal *devbuild.DevBuildInternalServerError
	require.ErrorAs(t, err, &internal)
	assert.Equal(t, 500, internal.Code)
	assert.Contains(t, internal.Message, "unable to trigger")
}

func TestCreateStoresTriggerEventIDs(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	env.cloudEvents.setEventID("evt-1")
	creator := "event-id@test"

	build, err := env.service.Create(context.Background(), gitRefCreatePayload(creator, "branch/master", false))
	require.NoError(t, err)
	require.NotNil(t, build.Status.TektonStatus)
	assert.Contains(t, build.Status.TektonStatus.TriggersEventIDs, "evt-1")
}
