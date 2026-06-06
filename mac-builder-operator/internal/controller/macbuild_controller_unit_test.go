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
	testWorkerA = "worker-a"
	testWorkerB = "worker-b"
)

func TestReconcileBuildingSuccessInitializesOutputs(t *testing.T) {
	t.Parallel()

	fixedNow := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "build-success",
			Namespace: "default",
		},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:     buildv1alpha1.PhaseBuilding,
			WorkerID:  stringPtr(testWorkerA),
			StartTime: &metav1.Time{Time: fixedNow.Add(-time.Minute)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
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
	if updated.Status.Outputs == nil || updated.Status.Outputs.PushedArtifactsYaml == nil {
		t.Fatalf("expected outputs.pushed_artifacts_yaml to be populated, got %#v", updated.Status.Outputs)
	}
	if *updated.Status.Outputs.PushedArtifactsYaml != "artifacts:\n- name: pd\n" {
		t.Fatalf("unexpected pushed artifacts yaml: %q", *updated.Status.Outputs.PushedArtifactsYaml)
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
			Phase:     buildv1alpha1.PhaseBuilding,
			WorkerID:  stringPtr(testWorkerB),
			StartTime: &metav1.Time{Time: fixedNow.Add(-10 * time.Minute)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
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
			Phase:     buildv1alpha1.PhaseBuilding,
			WorkerID:  stringPtr(testWorkerB),
			StartTime: &metav1.Time{Time: fixedNow.Add(-2 * time.Hour)},
		},
	}

	reconciler, k8sClient := newTestMacBuildReconciler(t, fixedNow, macBuild)
	reconciler.WorkerID = testWorkerA
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
