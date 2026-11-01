package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	testWorkerA    = "worker-a"
	testWorkerB    = "worker-b"
	testWorkerArch = buildv1alpha1.BuildArchAMD64
)

func TestReconcileIgnoresPendingBuild(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-pending", buildv1alpha1.PhasePending, "")
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhasePending {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhasePending, updated.Status.Phase)
	}
	if updated.Status.WorkerID != nil || updated.Status.Assignment != nil {
		t.Fatalf("agent must not claim Pending builds: %#v", updated.Status)
	}
}

func TestReconcileIgnoresEmptyPhase(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-empty", "", "")
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != "" {
		t.Fatalf("agent must not initialize the phase; got %q", updated.Status.Phase)
	}
}

func TestReconcileClaimsAssignedBuildForSelf(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-assigned", buildv1alpha1.PhaseAssigned, testWorkerA)
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhasePreparing {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhasePreparing, updated.Status.Phase)
	}
	if updated.Status.WorkerID == nil || *updated.Status.WorkerID != testWorkerA {
		t.Fatalf("expected worker ID %q, got %#v", testWorkerA, updated.Status.WorkerID)
	}
	if updated.Status.WorkerArch == nil || *updated.Status.WorkerArch != testWorkerArch {
		t.Fatalf("expected worker arch %q, got %#v", testWorkerArch, updated.Status.WorkerArch)
	}
	if updated.Status.StartTime == nil || !updated.Status.StartTime.Time.Equal(fixedNow) {
		t.Fatalf("expected start time %s, got %#v", fixedNow, updated.Status.StartTime)
	}
	if updated.Status.Assignment == nil || updated.Status.Assignment.LeaseDeadline != nil {
		t.Fatalf("expected assignment with cleared lease deadline, got %#v", updated.Status.Assignment)
	}
	if len(updated.Status.PhaseHistory) != 1 || updated.Status.PhaseHistory[0].Phase != buildv1alpha1.PhasePreparing {
		t.Fatalf("unexpected phase history: %#v", updated.Status.PhaseHistory)
	}
}

func TestReconcileIgnoresBuildAssignedToAnotherAgent(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-foreign", buildv1alpha1.PhaseAssigned, testWorkerB)
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseAssigned {
		t.Fatalf("foreign assignment was modified: %#v", updated.Status)
	}
	if updated.Status.WorkerID != nil {
		t.Fatalf("foreign assignment set workerID: %#v", updated.Status.WorkerID)
	}
}

func TestReconcileFailsAssignedBuildWithoutAssignment(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-no-assignment", buildv1alpha1.PhaseAssigned, "")
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "no assignment") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
}

func TestReconcileRunsPreparingBuildAssignedToSelf(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-run", buildv1alpha1.PhasePreparing, testWorkerA)
	macBuild.Status.WorkerID = stringPtr(testWorkerA)
	macBuild.Status.WorkerArch = stringPtr(testWorkerArch)
	macBuild.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Minute)}
	macBuild.Status.PhaseHistory = []buildv1alpha1.MacBuildPhaseHistoryEntry{{
		Phase:          buildv1alpha1.PhasePreparing,
		TransitionTime: metav1.NewTime(fixedNow.Add(-time.Minute)),
	}}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.runBuild = func(_ context.Context, _ buildv1alpha1.MacBuild, reportPhase buildPhaseReporter) (*buildResult, error) {
		if err := reportPhase(buildv1alpha1.PhaseBuilding, "Running build steps on the worker."); err != nil {
			return nil, err
		}
		if err := reportPhase(buildv1alpha1.PhasePublishing, "Publishing build artifacts."); err != nil {
			return nil, err
		}
		return &buildResult{
			CommitHash:          "deadbeef",
			PushedArtifactsYaml: "artifacts:\n- name: tidb\n",
		}, nil
	}

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseSucceeded {
		t.Fatalf("expected succeeded phase, got %q (%#v)", updated.Status.Phase, updated.Status.Message)
	}
	if updated.Status.CommitHash == nil || *updated.Status.CommitHash != "deadbeef" {
		t.Fatalf("unexpected commit hash: %#v", updated.Status.CommitHash)
	}
	if updated.Status.Outputs == nil || updated.Status.Outputs.PushedArtifactsYaml == nil {
		t.Fatalf("expected pushed artifacts, got %#v", updated.Status.Outputs)
	}
	phases := make([]string, 0, len(updated.Status.PhaseHistory))
	for _, entry := range updated.Status.PhaseHistory {
		phases = append(phases, entry.Phase)
	}
	for _, want := range []string{
		buildv1alpha1.PhasePreparing,
		buildv1alpha1.PhaseBuilding,
		buildv1alpha1.PhasePublishing,
		buildv1alpha1.PhaseSucceeded,
	} {
		if !containsString(phases, want) {
			t.Fatalf("phase history %v does not contain %q", phases, want)
		}
	}
}

func TestReconcileFailsActiveBuildWithoutAssignment(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-active-no-assignment", buildv1alpha1.PhaseBuilding, "")
	macBuild.Status.WorkerID = stringPtr(testWorkerA)
	macBuild.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Minute)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "without an assignment") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
}

func TestReconcileFailsActiveBuildWithMismatchedWorkerID(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-mismatched-worker", buildv1alpha1.PhaseBuilding, testWorkerA)
	macBuild.Status.WorkerID = stringPtr(testWorkerB)
	macBuild.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Minute)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "status.workerID") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
}

func TestReconcileFailsArchMismatch(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-arch-mismatch", buildv1alpha1.PhaseAssigned, testWorkerA)
	macBuild.Spec.Build.Arch = buildv1alpha1.BuildArchARM64
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "build arch") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
}

func TestReconcileFailsTimedOutBuild(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-timeout", buildv1alpha1.PhaseBuilding, testWorkerA)
	macBuild.Status.WorkerID = stringPtr(testWorkerA)
	macBuild.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-2 * time.Hour)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.BuildTimeout = time.Hour

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "timed out") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
}

func TestReconcileFailsInsufficientDisk(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := newTestBuild("build-disk", buildv1alpha1.PhasePreparing, testWorkerA)
	macBuild.Status.WorkerID = stringPtr(testWorkerA)
	macBuild.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Minute)}
	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.MinFreeDiskBytes = 20 * 1024 * 1024 * 1024
	reconciler.DiskFreeBytes = func(context.Context) int64 { return 1 }
	runCalled := false
	reconciler.runBuild = func(context.Context, buildv1alpha1.MacBuild, buildPhaseReporter) (*buildResult, error) {
		runCalled = true
		return &buildResult{}, nil
	}

	if _, err := reconciler.Reconcile(context.Background(), requestFor(macBuild)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	updated := getTestBuild(t, k8sClient, macBuild)
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed phase, got %q", updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, "insufficient disk") {
		t.Fatalf("unexpected message: %#v", updated.Status.Message)
	}
	if runCalled {
		t.Fatal("runBuild must not be called when disk is insufficient")
	}
}

func TestReconcilePrefersAPIReaderOverCachedClient(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)

	cachedObject := newTestBuild("build-stale-cache", buildv1alpha1.PhaseBuilding, testWorkerA)
	cachedObject.Status.WorkerID = stringPtr(testWorkerA)
	cachedObject.Status.StartTime = &metav1.Time{Time: fixedNow.Add(-time.Minute)}

	liveObject := cachedObject.DeepCopy()
	liveObject.Status.Phase = buildv1alpha1.PhaseSucceeded

	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add scheme: %v", err)
	}
	cachedClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&buildv1alpha1.MacBuild{}).
		WithObjects(cachedObject).
		Build()
	apiReader := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&buildv1alpha1.MacBuild{}).
		WithObjects(liveObject).
		Build()

	reconciler := &MacBuildReconciler{
		Client:     cachedClient,
		APIReader:  apiReader,
		Scheme:     scheme,
		WorkerID:   testWorkerA,
		WorkerArch: testWorkerArch,
		now:        func() time.Time { return fixedNow },
		runBuild: func(context.Context, buildv1alpha1.MacBuild, buildPhaseReporter) (*buildResult, error) {
			t.Fatal("runBuild should not be called: the live phase is Succeeded")
			return nil, nil
		},
	}

	result, err := reconciler.Reconcile(context.Background(), requestFor(cachedObject))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result != (ctrl.Result{}) {
		t.Fatalf("expected no requeue when the live phase is terminal, got %+v", result)
	}
}

func newTestBuild(name, phase, agent string) *buildv1alpha1.MacBuild {
	build := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
		Spec: buildv1alpha1.MacBuildSpec{
			Build: buildv1alpha1.BuildSpec{
				Component: "tidb",
				Version:   "nightly",
				Arch:      testWorkerArch,
			},
		},
		Status: buildv1alpha1.MacBuildStatus{Phase: phase},
	}
	if agent != "" {
		build.Status.Assignment = &buildv1alpha1.MacBuildAssignment{
			AgentName:  agent,
			AssignedAt: metav1.NewTime(time.Date(2026, 6, 6, 11, 0, 0, 0, time.UTC)),
		}
	}
	return build
}

func requestFor(build *buildv1alpha1.MacBuild) ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Name: build.Name, Namespace: build.Namespace}}
}

func getTestBuild(t *testing.T, k8sClient client.Client, build *buildv1alpha1.MacBuild) *buildv1alpha1.MacBuild {
	t.Helper()
	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(build), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	return &updated
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func newTestMacBuildReconciler(
	t *testing.T,
	fixedNow time.Time,
	objects ...client.Object,
) (*MacBuildReconciler, client.Client) {
	t.Helper()

	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add buildv1alpha1 scheme: %v", err)
	}

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&buildv1alpha1.MacBuild{}).
		WithObjects(objects...).
		Build()

	return &MacBuildReconciler{
		Client:            k8sClient,
		Scheme:            scheme,
		WorkerID:          testWorkerA,
		WorkerArch:        testWorkerArch,
		BuildTimeout:      time.Hour,
		BuildPollInterval: time.Minute,
		now: func() time.Time {
			return fixedNow
		},
	}, k8sClient
}

func stringPtr(value string) *string {
	return &value
}
