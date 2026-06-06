package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testWorkerA    = "worker-a"
	testWorkerB    = "worker-b"
	testWorkerArch = buildv1alpha1.BuildArchAMD64
)

func TestReconcileInitializesPendingStatusWithPhaseHistory(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "build-pending",
			Namespace: "default",
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	_, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: macBuild.Name, Namespace: macBuild.Namespace},
	})
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(macBuild), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	if updated.Status.Phase != buildv1alpha1.PhasePending {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhasePending, updated.Status.Phase)
	}
	if updated.Status.PhaseMessage == nil || *updated.Status.PhaseMessage == "" {
		t.Fatalf("expected phase message to be populated, got %#v", updated.Status.PhaseMessage)
	}
	if len(updated.Status.PhaseHistory) != 1 {
		t.Fatalf("expected one phase history entry, got %#v", updated.Status.PhaseHistory)
	}
	if updated.Status.PhaseHistory[0].Phase != buildv1alpha1.PhasePending {
		t.Fatalf("expected pending phase history entry, got %#v", updated.Status.PhaseHistory[0])
	}
	if !updated.Status.PhaseHistory[0].TransitionTime.Time.Equal(fixedNow) {
		t.Fatalf("expected pending transition time %s, got %#v", fixedNow, updated.Status.PhaseHistory[0].TransitionTime)
	}
}

func TestReconcilePendingClaimRecordsWorkerIdentityAndPhaseHistory(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	pendingAt := metav1.NewTime(fixedNow.Add(-time.Minute))
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "build-claim",
			Namespace: "default",
		},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:        buildv1alpha1.PhasePending,
			PhaseMessage: stringPtr("Waiting for a matching macOS worker to claim this build."),
			PhaseHistory: []buildv1alpha1.MacBuildPhaseHistoryEntry{
				{
					Phase:          buildv1alpha1.PhasePending,
					TransitionTime: pendingAt,
					Message:        stringPtr("Waiting for a matching macOS worker to claim this build."),
				},
			},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
	reconciler.WorkerArch = testWorkerArch

	_, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: macBuild.Name, Namespace: macBuild.Namespace},
	})
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(macBuild), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	if updated.Status.Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhaseBuilding, updated.Status.Phase)
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
	if updated.Status.PhaseMessage == nil || *updated.Status.PhaseMessage == "" {
		t.Fatalf("expected phase message to be populated, got %#v", updated.Status.PhaseMessage)
	}
	if len(updated.Status.PhaseHistory) != 2 {
		t.Fatalf("expected two phase history entries, got %#v", updated.Status.PhaseHistory)
	}
	if updated.Status.PhaseHistory[1].Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("expected second phase history entry to be Building, got %#v", updated.Status.PhaseHistory[1])
	}
}

func TestReconcileBuildingSuccessInitializesOutputs(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "build-success",
			Namespace: "default",
		},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:        buildv1alpha1.PhaseBuilding,
			PhaseMessage: stringPtr("Build claimed by worker and queued for execution."),
			PhaseHistory: []buildv1alpha1.MacBuildPhaseHistoryEntry{
				{
					Phase:          buildv1alpha1.PhaseBuilding,
					TransitionTime: metav1.NewTime(fixedNow.Add(-time.Minute)),
					Message:        stringPtr("Build claimed by worker and queued for execution."),
				},
			},
			WorkerID:  stringPtr(testWorkerA),
			StartTime: &metav1.Time{Time: fixedNow.Add(-time.Minute)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
	reconciler.WorkerArch = testWorkerArch
	reconciler.runBuild = func(context.Context, buildv1alpha1.MacBuild) (*buildResult, error) {
		return &buildResult{
			CommitHash:          "abc123",
			PushedArtifactsYaml: "artifacts:\n- name: pd\n",
		}, nil
	}

	result, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: macBuild.Name, Namespace: macBuild.Namespace},
	})
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result != (ctrl.Result{}) {
		t.Fatalf("expected reconcile to finish without requeue, got %+v", result)
	}

	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(macBuild), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	if updated.Status.Phase != buildv1alpha1.PhaseSucceeded {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhaseSucceeded, updated.Status.Phase)
	}
	if updated.Status.CommitHash == nil || *updated.Status.CommitHash != "abc123" {
		t.Fatalf("expected commit hash abc123, got %#v", updated.Status.CommitHash)
	}
	if updated.Status.WorkerArch == nil || *updated.Status.WorkerArch != testWorkerArch {
		t.Fatalf("expected worker arch %q, got %#v", testWorkerArch, updated.Status.WorkerArch)
	}
	if updated.Status.Outputs == nil || updated.Status.Outputs.PushedArtifactsYaml == nil {
		t.Fatalf("expected outputs.pushed_artifacts_yaml to be populated, got %#v", updated.Status.Outputs)
	}
	if *updated.Status.Outputs.PushedArtifactsYaml != "artifacts:\n- name: pd\n" {
		t.Fatalf("unexpected pushed artifacts yaml: %q", *updated.Status.Outputs.PushedArtifactsYaml)
	}
	if updated.Status.PhaseMessage == nil || *updated.Status.PhaseMessage != "Build completed successfully." {
		t.Fatalf("expected success phase message, got %#v", updated.Status.PhaseMessage)
	}
	if len(updated.Status.PhaseHistory) != 2 {
		t.Fatalf("expected two phase history entries, got %#v", updated.Status.PhaseHistory)
	}
	if updated.Status.PhaseHistory[1].Phase != buildv1alpha1.PhaseSucceeded {
		t.Fatalf("expected final phase history entry to be Succeeded, got %#v", updated.Status.PhaseHistory[1])
	}
	if updated.Status.CompletionTime == nil || !updated.Status.CompletionTime.Time.Equal(fixedNow) {
		t.Fatalf("expected completion time %s, got %#v", fixedNow, updated.Status.CompletionTime)
	}
}

func TestReconcileRequeuesForeignBuildBeforeTimeout(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "foreign-build",
			Namespace: "default",
		},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:        buildv1alpha1.PhaseBuilding,
			PhaseMessage: stringPtr("Build claimed by worker and queued for execution."),
			PhaseHistory: []buildv1alpha1.MacBuildPhaseHistoryEntry{
				{
					Phase:          buildv1alpha1.PhaseBuilding,
					TransitionTime: metav1.NewTime(fixedNow.Add(-10 * time.Minute)),
					Message:        stringPtr("Build claimed by worker and queued for execution."),
				},
			},
			WorkerID:  stringPtr(testWorkerB),
			StartTime: &metav1.Time{Time: fixedNow.Add(-10 * time.Minute)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
	reconciler.WorkerArch = testWorkerArch
	reconciler.BuildTimeout = time.Hour
	reconciler.BuildPollInterval = 2 * time.Minute
	reconciler.runBuild = func(context.Context, buildv1alpha1.MacBuild) (*buildResult, error) {
		t.Fatal("runBuild should not be called for a foreign build")
		return nil, nil
	}

	result, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: macBuild.Name, Namespace: macBuild.Namespace},
	})
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter != 2*time.Minute {
		t.Fatalf("expected requeue after 2m, got %+v", result)
	}

	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(macBuild), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	if updated.Status.Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("expected phase to remain %q, got %q", buildv1alpha1.PhaseBuilding, updated.Status.Phase)
	}
	if len(updated.Status.PhaseHistory) != 1 {
		t.Fatalf("expected phase history to remain unchanged, got %#v", updated.Status.PhaseHistory)
	}
}

func TestReconcileMarksStaleForeignBuildFailed(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "stale-build",
			Namespace: "default",
		},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:        buildv1alpha1.PhaseBuilding,
			PhaseMessage: stringPtr("Build claimed by worker and queued for execution."),
			PhaseHistory: []buildv1alpha1.MacBuildPhaseHistoryEntry{
				{
					Phase:          buildv1alpha1.PhaseBuilding,
					TransitionTime: metav1.NewTime(fixedNow.Add(-2 * time.Hour)),
					Message:        stringPtr("Build claimed by worker and queued for execution."),
				},
			},
			WorkerID:  stringPtr(testWorkerB),
			StartTime: &metav1.Time{Time: fixedNow.Add(-2 * time.Hour)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
	reconciler.WorkerArch = testWorkerArch
	reconciler.BuildTimeout = time.Hour
	reconciler.BuildPollInterval = 2 * time.Minute
	reconciler.runBuild = func(context.Context, buildv1alpha1.MacBuild) (*buildResult, error) {
		t.Fatal("runBuild should not be called for a stale foreign build")
		return nil, nil
	}

	result, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: macBuild.Name, Namespace: macBuild.Namespace},
	})
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result != (ctrl.Result{}) {
		t.Fatalf("expected stale build reconcile to finish without requeue, got %+v", result)
	}

	var updated buildv1alpha1.MacBuild
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(macBuild), &updated); err != nil {
		t.Fatalf("get updated MacBuild: %v", err)
	}
	if updated.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected phase %q, got %q", buildv1alpha1.PhaseFailed, updated.Status.Phase)
	}
	if updated.Status.Message == nil || !strings.Contains(*updated.Status.Message, `worker "`+testWorkerB+`"`) {
		t.Fatalf("expected timeout message to mention worker-b, got %#v", updated.Status.Message)
	}
	if updated.Status.PhaseMessage == nil || !strings.Contains(*updated.Status.PhaseMessage, `worker "`+testWorkerB+`"`) {
		t.Fatalf("expected phase message to mention worker-b, got %#v", updated.Status.PhaseMessage)
	}
	if len(updated.Status.PhaseHistory) != 2 {
		t.Fatalf("expected failed build to append phase history, got %#v", updated.Status.PhaseHistory)
	}
	if updated.Status.PhaseHistory[1].Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected final phase history entry to be Failed, got %#v", updated.Status.PhaseHistory[1])
	}
	if updated.Status.CompletionTime == nil || !updated.Status.CompletionTime.Time.Equal(fixedNow) {
		t.Fatalf("expected completion time %s, got %#v", fixedNow, updated.Status.CompletionTime)
	}
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
		Client: k8sClient,
		Scheme: scheme,
		now: func() time.Time {
			return fixedNow
		},
	}, k8sClient
}

func stringPtr(value string) *string {
	return &value
}
