package impl_test

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/registry"

	_ "github.com/mattn/go-sqlite3"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// newArtifact creates an artifact service backed by a temporary sqlite db with
// a fast worker polling rate (100ms). It returns the service and the db DSN.
func newArtifact(t *testing.T, cfgMods ...func(*config.Service)) (artifact.Service, string) {
	t.Helper()

	dsn := filepath.Join(t.TempDir(), "test.db") + "?_fk=1"
	cfg := &config.Service{
		Store: config.Store{
			Driver: "sqlite3",
			DSN:    dsn,
		},
		ImageSync: config.ImageSync{
			PollingRate: "100ms",
		},
	}
	for _, mod := range cfgMods {
		mod(cfg)
	}

	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	svc := impl.NewArtifact(&logger, cfg)
	require.NotNil(t, svc, "failed to initialize artifact service")
	return svc, dsn
}

// artifactWorker returns the Start/Stop methods of the artifact service so
// tests can drive the background worker.
func artifactWorker(t *testing.T, svc artifact.Service) interface {
	Start(context.Context)
	Stop()
} {
	t.Helper()
	w, ok := svc.(interface {
		Start(context.Context)
		Stop()
	})
	require.True(t, ok, "artifact service does not implement worker interface")
	return w
}

// waitForStatus polls the task status until it reaches one of the expected
// statuses or the timeout expires.
func waitForStatus(t *testing.T, svc artifact.Service, id int, expected []string, timeout time.Duration) *artifact.ImageSyncTask {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		task, err := svc.GetImageSyncTask(context.Background(), &artifact.GetImageSyncTaskPayload{ID: id})
		require.NoError(t, err)
		for _, s := range expected {
			if task.Status == s {
				return task
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("task %d did not reach any of %v within %s", id, expected, timeout)
	return nil
}

func TestListImageSyncTasks(t *testing.T) {
	svc, dsn := newArtifact(t)

	db, err := ent.Open("sqlite3", dsn)
	require.NoError(t, err)
	defer db.Close()

	statuses := []string{"PENDING", "SUCCEEDED", "FAILED", "PROCESSING", "SUCCEEDED"}
	for i, st := range statuses {
		task, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: fmt.Sprintf("src/req-%d:tag", i),
			Target: fmt.Sprintf("dst/req-%d:tag", i),
		})
		require.NoError(t, err)
		// Flip the status directly to simulate a processed task.
		_, err = db.ImageSyncTask.UpdateOneID(task.ID).SetStatus(st).Save(context.Background())
		require.NoError(t, err)
	}

	t.Run("list all with pagination", func(t *testing.T) {
		resp, err := svc.ListImageSyncTasks(context.Background(), &artifact.ListImageSyncTasksPayload{
			Page:      1,
			PageSize:  2,
			Sort:      "createdAt",
			Direction: "desc",
		})
		require.NoError(t, err)
		require.Len(t, resp, 2)
		assert.Equal(t, 5, resp[0].ID)
		assert.Equal(t, 4, resp[1].ID)
	})

	t.Run("filter by status", func(t *testing.T) {
		resp, err := svc.ListImageSyncTasks(context.Background(), &artifact.ListImageSyncTasksPayload{
			Page:     1,
			PageSize: 30,
			Status:   stringPtr("SUCCEEDED"),
		})
		require.NoError(t, err)
		require.Len(t, resp, 2)
		for _, task := range resp {
			assert.Equal(t, "SUCCEEDED", task.Status)
		}
	})

	t.Run("sort asc", func(t *testing.T) {
		resp, err := svc.ListImageSyncTasks(context.Background(), &artifact.ListImageSyncTasksPayload{
			Page:      1,
			PageSize:  30,
			Sort:      "updatedAt",
			Direction: "asc",
		})
		require.NoError(t, err)
		require.Len(t, resp, 5)
		assert.Equal(t, 1, resp[0].ID)
	})

	t.Run("empty result for status without matches", func(t *testing.T) {
		resp, err := svc.ListImageSyncTasks(context.Background(), &artifact.ListImageSyncTasksPayload{
			Page:     1,
			PageSize: 30,
			Status:   stringPtr("FAILED"),
		})
		require.NoError(t, err)
		assert.Len(t, resp, 1)
		assert.Equal(t, "FAILED", resp[0].Status)
	})
}

func stringPtr(s string) *string { return &s }

func TestSyncImage_Validation(t *testing.T) {
	t.Run("no rules configured skips validation", func(t *testing.T) {
		svc, _ := newArtifact(t)
		resp, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: "whatever/image:tag",
			Target: "whatever/image:tag",
		})
		assert.NoError(t, err)
		require.NotNil(t, resp)
		assert.Equal(t, "PENDING", resp.Status)
	})

	t.Run("rules enforced from config", func(t *testing.T) {
		svc, _ := newArtifact(t, func(cfg *config.Service) {
			cfg.ImageSync.SourceRegx = `^hub\.pingcap\.net/(pingcap|tikv)/[\w-/]+:v\d+\.\d+\.\d+-\d{8,}.*$`
			cfg.ImageSync.TargetRegx = `^((docker\.io/)?pingcap/[\w-]+|gcr\.io/pingcap-public/dbaas/[\w-]+):v\d+\.\d+\.\d+-\d{8,}.*$`
		})

		_, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: "docker.io/library/alpine:latest",
			Target: "pingcap/tidb:v6.5.0-20240101000000",
		})
		require.Error(t, err)
		httpErr, ok := err.(*artifact.HTTPError)
		require.True(t, ok)
		assert.Equal(t, 400, httpErr.Code)
		assert.Contains(t, httpErr.Message, "source image not valid")

		_, err = svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: "hub.pingcap.net/pingcap/tidb:v6.5.0-20240101000000",
			Target: "docker.io/library/alpine:latest",
		})
		require.Error(t, err)
		httpErr, ok = err.(*artifact.HTTPError)
		require.True(t, ok)
		assert.Equal(t, 400, httpErr.Code)
		assert.Contains(t, httpErr.Message, "target image not valid")

		resp, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: "hub.pingcap.net/pingcap/tidb:v6.5.0-20240101000000",
			Target: "pingcap/tidb:v6.5.0-20240101000000",
		})
		assert.NoError(t, err)
		require.NotNil(t, resp)
		assert.Equal(t, "PENDING", resp.Status)
	})
}

func TestSyncImage_ConfigHotReload(t *testing.T) {
	dsn := filepath.Join(t.TempDir(), "test.db") + "?_fk=1"

	writeConfig := func(content string) {
		path := filepath.Join(filepath.Dir(dsn), "config.yaml")
		require.NoError(t, os.WriteFile(path, []byte(content), 0o600))
	}

	storeBlock := fmt.Sprintf("store:\n  driver: sqlite3\n  dsn: %q\n", dsn)
	writeConfig(storeBlock)

	cfg, err := config.Load(filepath.Join(filepath.Dir(dsn), "config.yaml"))
	require.NoError(t, err)

	logger := zerolog.New(zerolog.NewConsoleWriter()).With().Timestamp().Logger()
	svc := impl.NewArtifact(&logger, cfg.Get())
	require.NotNil(t, svc)
	reloader, ok := svc.(interface{ Reload(*config.Service) })
	require.True(t, ok)
	cfg.OnReload(reloader.Reload)

	// No rules yet: validation is skipped.
	_, err = svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
		Source: "foo/bar:tag",
		Target: "foo/bar:tag",
	})
	assert.NoError(t, err)

	// Update the config file on disk and trigger a reload.
	writeConfig(storeBlock + "image_sync:\n  source_regx: \"^hub\\\\.pingcap\\\\.net/\"\n  target_regx: \"^pingcap/\"\n")
	require.NoError(t, cfg.Reload())

	// New rules are enforced after reload.
	_, err = svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
		Source: "foo/bar:tag",
		Target: "pingcap/tidb:v6.5.0-20240101000000",
	})
	require.Error(t, err)
	httpErr, ok := err.(*artifact.HTTPError)
	require.True(t, ok)
	assert.Equal(t, 400, httpErr.Code)
	assert.Contains(t, httpErr.Message, "source image not valid")
}

func TestSyncImage_Integration(t *testing.T) {
	// Skip if running in CI or want to skip integration tests
	if testing.Short() {
		t.Skip("Skipping integration test")
	}

	ctx := context.Background()

	// Start the registry container
	registryContainer, err := registry.Run(ctx, "registry:2.8.3")
	if err != nil {
		log.Fatalf("failed to start registry container: %v", err)
	}

	t.Cleanup(func() {
		if err := testcontainers.TerminateContainer(registryContainer); err != nil {
			log.Fatalf("failed to terminate registry container: %v", err)
		}
	})

	// Get the registry URL (e.g., "localhost:5000")
	registryURL, err := registryContainer.HostAddress(t.Context())
	if err != nil {
		t.Fatal(err)
	}

	svc, _ := newArtifact(t)
	worker := artifactWorker(t, svc)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go worker.Start(ctx)
	defer worker.Stop()

	t.Run("sync real image", func(t *testing.T) {
		// Use a small image for faster tests
		sourceImage := "alpine:latest"

		// Use a unique name to avoid conflicts
		randomSuffix := time.Now().UnixNano()
		targetImage := fmt.Sprintf("%s/test-sync-%d:1h", registryURL, randomSuffix)

		task, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: sourceImage,
			Target: targetImage,
		})
		require.NoError(t, err)
		require.NotNil(t, task)
		assert.Equal(t, "PENDING", task.Status)

		final := waitForStatus(t, svc, task.ID, []string{"SUCCEEDED", "FAILED"}, 2*time.Minute)
		assert.Equal(t, "SUCCEEDED", final.Status, "task should succeed, got error: %v", final.ErrorMessage)
		t.Logf("Successfully pushed image to: %s", targetImage)
	})

	t.Run("sync non-existent image should fail", func(t *testing.T) {
		sourceImage := "debian:non-existent-tag-12345"
		targetImage := fmt.Sprintf("%s/test-sync-should-fail:non-existent-tag-12345", registryURL)

		task, err := svc.SyncImage(context.Background(), &artifact.ImageSyncRequest{
			Source: sourceImage,
			Target: targetImage,
		})
		require.NoError(t, err)
		require.NotNil(t, task)
		assert.Equal(t, "PENDING", task.Status)

		// Worker retries (3 attempts) then gives up.
		final := waitForStatus(t, svc, task.ID, []string{"SUCCEEDED", "FAILED"}, 2*time.Minute)
		assert.Equal(t, "FAILED", final.Status)
		require.NotNil(t, final.ErrorMessage)
		assert.NotEmpty(t, *final.ErrorMessage)
	})
}

func TestWorker_ReclaimsInterruptedTasks(t *testing.T) {
	if testing.Short() {
		t.Skip("Skipping integration test")
	}

	ctx := context.Background()

	// The service creates the schema on construction; the worker is started
	// afterwards and must reclaim tasks left in PROCESSING by a previous run.
	svc, dsn := newArtifact(t)

	// Create a task and simulate an interruption by marking it PROCESSING
	// directly in the database (as if the service died mid-copy).
	task, err := svc.SyncImage(ctx, &artifact.ImageSyncRequest{
		Source: "debian:non-existent-tag-12345",
		Target: "not-a-registry.invalid/foo:bar",
	})
	require.NoError(t, err)

	db, err := ent.Open("sqlite3", dsn)
	require.NoError(t, err)
	defer db.Close()
	_, err = db.ImageSyncTask.UpdateOneID(task.ID).
		SetStatus("PROCESSING").
		Save(ctx)
	require.NoError(t, err)

	// Start the worker: it should reclaim the PROCESSING task and finish it.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	worker := artifactWorker(t, svc)
	go worker.Start(ctx)
	defer worker.Stop()

	final := waitForStatus(t, svc, task.ID, []string{"SUCCEEDED", "FAILED"}, 30*time.Second)
	assert.Equal(t, "FAILED", final.Status, "interrupted task should be reclaimed and finish")
}
