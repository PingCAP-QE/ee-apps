package impl_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"

	// Import SQLite driver
	_ "github.com/mattn/go-sqlite3"
)

type testEnv struct {
	cfg              *config.Service
	cloudEventServer *httptest.Server
	jenkinsServer    *httptest.Server
	service          devbuild.Service
}

func setupTestEnv(t *testing.T) *testEnv {
	// Setup fake cloudevent server
	cloudEventServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// Setup fake jenkins server
	jenkinsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// Setup config
	cfg := &config.Service{
		Store: config.Store{
			Driver: "sqlite3",
			DSN:    "file::memory:?cache=shared&_fk=1",
		},
		Tekton: config.Tekton{
			CloudeventEndpoint: cloudEventServer.URL,
		},
		Github: config.Github{
			Token: "fake-token",
		},
		Jenkins: config.Jenkins{
			URL:      jenkinsServer.URL,
			Username: "username",
			Password: "password",
			JobName:  "test-build",
		},
	}

	logger := zerolog.New(os.Stdout)
	service := impl.NewDevbuild(&logger, cfg)
	require.NotNil(t, service)

	return &testEnv{
		cfg:              cfg,
		cloudEventServer: cloudEventServer,
		jenkinsServer:    jenkinsServer,
		service:          service,
	}
}

func teardownTestEnv(t *testEnv) {
	t.cloudEventServer.Close()
	t.jenkinsServer.Close()
}

func TestDevBuildCRUD(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()

	t.Run("Create", func(t *testing.T) {
		// Test create with dry run
		createPayload := &devbuild.CreatePayload{
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v6.1.0",
				GitRef:  "branch/master",
			},
			Dryrun: true,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.Equal(t, "pd", build.Spec.Product)
		assert.Equal(t, "community", build.Spec.Edition)
		assert.Equal(t, "v6.1.0", build.Spec.Version)
	})

	t.Run("List", func(t *testing.T) {
		listPayload := &devbuild.ListPayload{
			Page:      1,
			PageSize:  10,
			Sort:      "created_at",
			Direction: "desc",
		}

		builds, err := env.service.List(ctx, listPayload)
		t.Log("err", err)
		require.NoError(t, err)
		assert.NotNil(t, builds)
	})

	t.Run("Get", func(t *testing.T) {
		// First create a build
		createPayload := &devbuild.CreatePayload{
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v6.1.0",
				GitRef:  "master",
			},
			Dryrun: true,
		}

		created, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)

		// Then get it
		getPayload := &devbuild.GetPayload{
			ID:   created.ID,
			Sync: false,
		}

		build, err := env.service.Get(ctx, getPayload)
		require.NoError(t, err)
		assert.Equal(t, created.ID, build.ID)
	})

	t.Run("Update", func(t *testing.T) {
		// First create a build
		createPayload := &devbuild.CreatePayload{
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v6.1.0",
				GitRef:  "master",
			},
			Dryrun: true,
		}

		created, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)

		// Then update it
		status := devbuild.BuildStatus("success")
		updatePayload := &devbuild.UpdatePayload{
			ID: created.ID,
			Status: &devbuild.DevBuildStatus{
				Status: status,
			},
		}

		updated, err := env.service.Update(ctx, updatePayload)
		require.NoError(t, err)
		assert.Equal(t, status, updated.Status.Status)
	})
}

func TestTriggerBuild(t *testing.T) {
	t.Run("Tekton Engine", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		// Modify the cloud event server to verify it receives the correct request
		receivedRequest := false
		env.cloudEventServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			receivedRequest = true
			// Verify content type and other headers
			assert.Equal(t, "application/json", r.Header.Get("Content-Type"))
			w.WriteHeader(http.StatusOK)
		})

		ctx := context.Background()
		engine := "tekton"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product:        "pd",
				Edition:        "community",
				Version:        "v6.1.0",
				GitRef:         "branch/master",
				PipelineEngine: &engine, // Explicitly use tekton engine
			},
			Dryrun: false, // Important: Not dry run to actually trigger the build
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.True(t, receivedRequest, "Cloud event request should have been sent")
		assert.Equal(t, devbuild.BuildStatus("pending"), build.Status.Status)
	})

	t.Run("Jenkins Engine", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		// Modify the Jenkins server to verify it receives the correct request
		receivedRequest := false
		env.jenkinsServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			receivedRequest = true
			// The Jenkins API path should include "build" for triggering a job
			assert.Contains(t, r.URL.Path, "/build")
			w.WriteHeader(http.StatusOK)
		})

		ctx := context.Background()
		engine := "jenkins"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product:        "pd",
				Edition:        "community",
				Version:        "v6.1.0",
				GitRef:         "branch/master",
				PipelineEngine: &engine, // Explicitly use jenkins engine
			},
			Dryrun: false, // Important: Not dry run to actually trigger the build
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)
		assert.True(t, receivedRequest, "Jenkins API request should have been sent")
		assert.Equal(t, devbuild.BuildStatus("pending"), build.Status.Status)
	})

	t.Run("Default Engine Selection", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		// Both servers should track if they received a request
		tektonReceived := false
		jenkinsReceived := false

		env.cloudEventServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tektonReceived = true
			w.WriteHeader(http.StatusOK)
		})

		env.jenkinsServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			jenkinsReceived = true
			w.WriteHeader(http.StatusOK)
		})

		ctx := context.Background()
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product: "pd",
				Edition: "community",
				Version: "v6.1.0",
				GitRef:  "branch/master",
				// No engine specified, should use default
			},
			Dryrun: false,
		}

		build, err := env.service.Create(ctx, createPayload)
		require.NoError(t, err)
		assert.NotNil(t, build)

		// Based on the symbol list, it appears tekton is the default engine
		// Only one of these should be true based on the default engine implementation
		if tektonReceived {
			assert.False(t, jenkinsReceived, "Only Tekton should have been triggered for default engine")
		} else {
			assert.True(t, jenkinsReceived, "When not using Tekton, Jenkins should have been triggered")
		}
	})

	t.Run("Error Handling", func(t *testing.T) {
		env := setupTestEnv(t)
		defer teardownTestEnv(env)

		// Make the servers return errors
		env.cloudEventServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		})

		env.jenkinsServer.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		})

		ctx := context.Background()
		engine := "unknown"
		createPayload := &devbuild.CreatePayload{
			CreatedBy: "test-user",
			Request: &devbuild.DevBuildSpec{
				Product:        "pd",
				Edition:        "community",
				Version:        "v6.1.0",
				GitRef:         "branch/master",
				PipelineEngine: &engine,
			},
			Dryrun: false,
		}

		// The build should be created but with an error status
		_, err := env.service.Create(ctx, createPayload)

		// We expect an error because the cloud event/Jenkins server returns 500
		assert.Error(t, err)

		// Even with a trigger error, we should be able to list the build
		listPayload := &devbuild.ListPayload{
			Page:     1,
			PageSize: 10,
		}

		builds, err := env.service.List(ctx, listPayload)
		require.NoError(t, err)
		assert.GreaterOrEqual(t, len(builds), 1)
	})
}

func TestDevBuildRerun(t *testing.T) {
	env := setupTestEnv(t)
	defer teardownTestEnv(env)

	ctx := context.Background()

	// First create a build
	createPayload := &devbuild.CreatePayload{
		CreatedBy: "test-user",
		Request: &devbuild.DevBuildSpec{
			Product: "pd",
			Edition: "community",
			Version: "v6.1.0",
			GitRef:  "branch/master",
		},
		Dryrun: true,
	}

	created, err := env.service.Create(ctx, createPayload)
	require.NoError(t, err)

	// Then rerun it
	rerunPayload := &devbuild.RerunPayload{
		ID: created.ID,
	}

	rerun, err := env.service.Rerun(ctx, rerunPayload)
	require.NoError(t, err)
	assert.NotEqual(t, created.ID, rerun.ID)
	assert.Equal(t, created.Spec.Product, rerun.Spec.Product)
	assert.Equal(t, created.Spec.Edition, rerun.Spec.Edition)
	assert.Equal(t, created.Spec.Version, rerun.Spec.Version)
}
