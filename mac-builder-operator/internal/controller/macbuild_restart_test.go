package controller

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/state"
)

func newRestartState(t *testing.T, reconciler *MacBuildReconciler, persisted state.State) *state.Store {
	t.Helper()
	store := state.NewStore(filepath.Join(t.TempDir(), "state.json"))
	if err := store.Save(persisted); err != nil {
		t.Fatalf("save state: %v", err)
	}
	reconciler.StateStore = store
	return store
}

func TestReconcileRestartFailsPublishingBuild(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	build := newTestBuild("build-publishing", buildv1alpha1.PhasePublishing, testWorkerA)
	build.Status.WorkerID = stringPtr(testWorkerA)
	build.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Hour)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, build)
	store := newRestartState(t, reconciler, state.State{
		Namespace: "default",
		Name:      "build-publishing",
		Phase:     buildv1alpha1.PhasePublishing,
		StartedAt: fixedNow.Add(-time.Hour),
	})

	if err := reconciler.reconcileRestart(context.Background()); err != nil {
		t.Fatalf("reconcile restart: %v", err)
	}

	updated := getTestBuild(t, k8sClient, build)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "restarted while publishing") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
	if persisted, err := store.Load(); err != nil || persisted != nil {
		t.Fatalf("expected state cleared, got %#v (err=%v)", persisted, err)
	}
}

func TestReconcileRestartBumpsRestartsForBuilding(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	build := newTestBuild("build-building", buildv1alpha1.PhaseBuilding, testWorkerA)
	build.Status.WorkerID = stringPtr(testWorkerA)
	build.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Hour)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, build)
	store := newRestartState(t, reconciler, state.State{
		Namespace: "default",
		Name:      "build-building",
		Phase:     buildv1alpha1.PhaseBuilding,
		StartedAt: fixedNow.Add(-time.Hour),
	})

	if err := reconciler.reconcileRestart(context.Background()); err != nil {
		t.Fatalf("reconcile restart: %v", err)
	}

	updated := getTestBuild(t, k8sClient, build)
	if updated.Status.Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("expected build to remain Building for a rerun, got %q", updated.Status.Phase)
	}
	if updated.Status.Assignment == nil || updated.Status.Assignment.Restarts != 1 {
		t.Fatalf("expected restarts=1, got %#v", updated.Status.Assignment)
	}
	if persisted, err := store.Load(); err != nil || persisted != nil {
		t.Fatalf("expected state cleared, got %#v (err=%v)", persisted, err)
	}
}

func TestReconcileRestartClearsTerminalState(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	build := newTestBuild("build-succeeded", buildv1alpha1.PhaseSucceeded, testWorkerA)
	reconciler, _ := newTestMacBuildReconciler(t, fixedNow, build)
	store := newRestartState(t, reconciler, state.State{
		Namespace: "default",
		Name:      "build-succeeded",
		Phase:     buildv1alpha1.PhaseBuilding,
	})

	if err := reconciler.reconcileRestart(context.Background()); err != nil {
		t.Fatalf("reconcile restart: %v", err)
	}
	if persisted, err := store.Load(); err != nil || persisted != nil {
		t.Fatalf("expected state cleared, got %#v (err=%v)", persisted, err)
	}
}

func TestReconcileRestartClearsReassignedBuild(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	build := newTestBuild("build-reassigned", buildv1alpha1.PhaseBuilding, testWorkerB)
	build.Status.WorkerID = stringPtr(testWorkerB)
	build.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Hour)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, build)
	store := newRestartState(t, reconciler, state.State{
		Namespace: "default",
		Name:      "build-reassigned",
		Phase:     buildv1alpha1.PhaseBuilding,
	})

	if err := reconciler.reconcileRestart(context.Background()); err != nil {
		t.Fatalf("reconcile restart: %v", err)
	}
	updated := getTestBuild(t, k8sClient, build)
	if updated.Status.Phase != buildv1alpha1.PhaseBuilding || updated.Status.Assignment.Restarts != 0 {
		t.Fatalf("reassigned build was modified: %#v", updated.Status)
	}
	if persisted, err := store.Load(); err != nil || persisted != nil {
		t.Fatalf("expected state cleared, got %#v (err=%v)", persisted, err)
	}
}

func TestReconcileRestartDiscardsCorruptState(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	build := newTestBuild("build-any", buildv1alpha1.PhasePending, "")
	reconciler, _ := newTestMacBuildReconciler(t, fixedNow, build)
	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("{"), 0o600); err != nil {
		t.Fatalf("write corrupt state: %v", err)
	}
	reconciler.StateStore = state.NewStore(path)

	if err := reconciler.reconcileRestart(context.Background()); err != nil {
		t.Fatalf("reconcile restart: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("expected corrupt state to be removed, got %v", err)
	}
}
