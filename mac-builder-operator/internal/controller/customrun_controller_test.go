package controller

import (
	"context"
	"testing"
	"time"

	tektonv1beta1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"knative.dev/pkg/apis"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	testGitURL    = "https://github.com/tikv/pd.git"
	testGitRef    = "refs/heads/master"
	testGitSha    = "0123456789abcdef0123456789abcdef01234567"
	testRefspec   = "refs/pull/123/head"
	testComponent = "pd"
	testVersion   = "v8.5.5"
	testOS        = "darwin"
	testArch      = "arm64"
	testProfile   = "release"
	testRegistry  = "hub.pingcap.net/devbuild"

	testMacBuildNamespace = "mac-builds"
	testCustomRunNS       = "ee-cd"
)

const (
	paramGitURL    = "git-url"
	paramGitRef    = "git-ref"
	paramGitRev    = "git-revision"
	paramRefspec   = "git-refspec"
	paramComponent = "component"
	paramVersion   = "version"
	paramOS        = "os"
	paramArch      = "arch"
	paramProfile   = "profile"
	paramPush      = "push"
	paramRegistry  = "registry"
	paramTTL       = "ttl-seconds-after-finished"
)

func newTestMacBuildCustomRun(name string, params map[string]string) *tektonv1beta1.CustomRun {
	items := make([]tektonv1beta1.Param, 0, len(params))
	for k, v := range params {
		items = append(items, tektonv1beta1.Param{
			Name:  k,
			Value: tektonv1beta1.ParamValue{Type: tektonv1beta1.ParamTypeString, StringVal: v},
		})
	}
	return &tektonv1beta1.CustomRun{
		ObjectMeta: metav1.ObjectMeta{Namespace: testCustomRunNS, Name: name},
		Spec: tektonv1beta1.CustomRunSpec{
			CustomRef: &tektonv1beta1.TaskRef{APIVersion: macBuildAPIVersion, Kind: macBuildKind, Name: name},
			Params:    items,
		},
	}
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add build scheme: %v", err)
	}
	if err := tektonv1beta1.AddToScheme(scheme); err != nil {
		t.Fatalf("add tekton scheme: %v", err)
	}
	return scheme
}

func newTestCustomRunReconciler(t *testing.T, objs ...client.Object) (*MacBuildCustomRunReconciler, client.Client) {
	t.Helper()
	scheme := testScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objs...).
		WithStatusSubresource(&tektonv1beta1.CustomRun{}).
		Build()
	return &MacBuildCustomRunReconciler{
		Client:            c,
		Scheme:            scheme,
		MacBuildNamespace: testMacBuildNamespace,
	}, c
}

func customRunRequest(cr *tektonv1beta1.CustomRun) ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}
}

func TestIsMacBuildCustomRun(t *testing.T) {
	t.Parallel()

	ours := newTestMacBuildCustomRun("cr-1", nil)
	if !isMacBuildCustomRun(ours) {
		t.Fatalf("expected CustomRun with MacBuild customRef to be recognized")
	}

	other := newTestMacBuildCustomRun("cr-2", nil)
	other.Spec.CustomRef = &tektonv1beta1.TaskRef{APIVersion: "example.com/v1", Kind: "SomethingElse", Name: "x"}
	if isMacBuildCustomRun(other) {
		t.Fatalf("expected non-MacBuild customRef to be ignored")
	}

	noRef := newTestMacBuildCustomRun("cr-3", nil)
	noRef.Spec.CustomRef = nil
	if isMacBuildCustomRun(noRef) {
		t.Fatalf("expected nil customRef to be ignored")
	}
}

func TestBuildMacBuildFromCustomRun(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("bp-pd-release-darwin-arm64-abcde-build-binaries", map[string]string{
		paramGitURL:    testGitURL,
		paramGitRef:    testGitRef,
		paramGitRev:    testGitSha,
		paramRefspec:   testRefspec,
		paramComponent: testComponent,
		paramVersion:   testVersion,
		paramOS:        testOS,
		paramArch:      "ARM64",
		paramProfile:   testProfile,
		paramPush:      "true",
		paramRegistry:  testRegistry,
		paramTTL:       "86400",
	})

	mb, err := buildMacBuildFromCustomRun(cr, testMacBuildNamespace, "macbuild-1")
	if err != nil {
		t.Fatalf("buildMacBuildFromCustomRun failed: %v", err)
	}

	if mb.Namespace != testMacBuildNamespace || mb.Name != "macbuild-1" {
		t.Fatalf("unexpected object identity: %s/%s", mb.Namespace, mb.Name)
	}
	if mb.Labels[labelCustomRunName] != cr.GetName() || mb.Labels[labelCustomRunNamespace] != testCustomRunNS {
		t.Fatalf("missing customrun labels: %#v", mb.Labels)
	}
	if mb.Spec.Source.GitRepository != testGitURL {
		t.Fatalf("unexpected git repository: %q", mb.Spec.Source.GitRepository)
	}
	if mb.Spec.Source.GitRef != testGitRef {
		t.Fatalf("unexpected git ref: %q", mb.Spec.Source.GitRef)
	}
	if mb.Spec.Source.GitSha == nil || *mb.Spec.Source.GitSha != testGitSha {
		t.Fatalf("unexpected git sha: %#v", mb.Spec.Source.GitSha)
	}
	if mb.Spec.Source.GitRefspec == nil || *mb.Spec.Source.GitRefspec != testRefspec {
		t.Fatalf("unexpected git refspec: %#v", mb.Spec.Source.GitRefspec)
	}
	if mb.Spec.Build.Component != testComponent || mb.Spec.Build.Version != testVersion {
		t.Fatalf("unexpected build: %#v", mb.Spec.Build)
	}
	if mb.Spec.Build.OS != testOS {
		t.Fatalf("unexpected os: %q", mb.Spec.Build.OS)
	}
	if mb.Spec.Build.Arch != buildv1alpha1.BuildArchARM64 {
		t.Fatalf("expected normalized arch arm64, got %q", mb.Spec.Build.Arch)
	}
	if mb.Spec.Build.Profile != testProfile {
		t.Fatalf("unexpected profile: %q", mb.Spec.Build.Profile)
	}
	if !mb.Spec.Artifacts.Push || mb.Spec.Artifacts.Registry != testRegistry {
		t.Fatalf("unexpected artifacts spec: %#v", mb.Spec.Artifacts)
	}
	if mb.Spec.TtlSecondsAfterFinished == nil || *mb.Spec.TtlSecondsAfterFinished != 86400 {
		t.Fatalf("unexpected ttl: %#v", mb.Spec.TtlSecondsAfterFinished)
	}
}

func TestBuildMacBuildFromCustomRunDefaults(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-defaults", map[string]string{
		paramGitURL:    testGitURL,
		paramComponent: testComponent,
	})

	mb, err := buildMacBuildFromCustomRun(cr, "default", "mb")
	if err != nil {
		t.Fatalf("buildMacBuildFromCustomRun failed: %v", err)
	}
	if mb.Spec.Build.OS != testOS {
		t.Fatalf("expected default os %q, got %q", testOS, mb.Spec.Build.OS)
	}
	if mb.Spec.Build.Profile != testProfile {
		t.Fatalf("expected default profile %q, got %q", testProfile, mb.Spec.Build.Profile)
	}
	if mb.Spec.Artifacts.Push {
		t.Fatalf("expected push to default false")
	}
}

func TestBuildMacBuildFromCustomRunMissingRequired(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-missing", map[string]string{
		paramComponent: testComponent,
	})
	if _, err := buildMacBuildFromCustomRun(cr, "default", "mb"); err == nil {
		t.Fatalf("expected error for missing git-url")
	}
}

func TestMapPhaseToCondition(t *testing.T) {
	t.Parallel()

	failedMsg := "boom"
	cases := []struct {
		name       string
		phase      string
		message    *string
		wantStatus corev1.ConditionStatus
		wantReason string
	}{
		{"empty", "", nil, corev1.ConditionUnknown, reasonPending},
		{"pending", buildv1alpha1.PhasePending, nil, corev1.ConditionUnknown, reasonRunning},
		{"building", buildv1alpha1.PhaseBuilding, nil, corev1.ConditionUnknown, reasonRunning},
		{"succeeded", buildv1alpha1.PhaseSucceeded, nil, corev1.ConditionTrue, reasonSucceeded},
		{"failed-with-message", buildv1alpha1.PhaseFailed, &failedMsg, corev1.ConditionFalse, reasonFailed},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			status, reason, message := mapPhaseToCondition(buildv1alpha1.MacBuildStatus{
				Phase:   tc.phase,
				Message: tc.message,
			})
			if status != tc.wantStatus || reason != tc.wantReason {
				t.Fatalf("mapPhaseToCondition(%q) = (%s,%s), want (%s,%s)", tc.phase, status, reason, tc.wantStatus, tc.wantReason)
			}
			if tc.phase == buildv1alpha1.PhaseFailed && message != failedMsg {
				t.Fatalf("expected failure message %q, got %q", failedMsg, message)
			}
		})
	}
}

func TestReconcileIgnoresOtherCustomRuns(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-other", nil)
	cr.Spec.CustomRef = &tektonv1beta1.TaskRef{APIVersion: "example.com/v1", Kind: "Other", Name: "x"}

	r, c := newTestCustomRunReconciler(t, cr)
	if _, err := r.Reconcile(context.Background(), customRunRequest(cr)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var mb buildv1alpha1.MacBuild
	err := c.Get(context.Background(), types.NamespacedName{Namespace: testMacBuildNamespace, Name: cr.Name}, &mb)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("expected no MacBuild to be created, got err=%v", err)
	}
}

func TestReconcileCreatesMacBuild(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-create", map[string]string{
		paramGitURL:    testGitURL,
		paramComponent: testComponent,
		paramVersion:   testVersion,
	})

	r, c := newTestCustomRunReconciler(t, cr)
	result, err := r.Reconcile(context.Background(), customRunRequest(cr))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter == 0 {
		t.Fatalf("expected a requeue while the MacBuild runs")
	}

	var mb buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: testMacBuildNamespace, Name: cr.Name}, &mb); err != nil {
		t.Fatalf("expected MacBuild created: %v", err)
	}
	if mb.Spec.Build.Component != testComponent {
		t.Fatalf("unexpected component: %q", mb.Spec.Build.Component)
	}
	if mb.Labels[labelCustomRunName] != cr.Name {
		t.Fatalf("expected customrun label, got %#v", mb.Labels)
	}
}

func TestReconcileSyncsSucceededStatus(t *testing.T) {
	t.Parallel()

	pushed := "oci:\n  repo: hub.pingcap.net/devbuild/tikv/pd/package\n  tag: v8.5.5\nfiles:\n  - pd-v8.5.5-darwin-arm64.tar.gz\n"
	cr := newTestMacBuildCustomRun("cr-sync", nil)
	mb := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: cr.Name, Namespace: testMacBuildNamespace},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:   buildv1alpha1.PhaseSucceeded,
			Outputs: &buildv1alpha1.MacBuildResultOutputs{PushedArtifactsYaml: &pushed},
		},
	}

	r, c := newTestCustomRunReconciler(t, cr, mb)
	if _, err := r.Reconcile(context.Background(), customRunRequest(cr)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var out tektonv1beta1.CustomRun
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}, &out); err != nil {
		t.Fatalf("get CustomRun: %v", err)
	}
	cond := out.Status.GetCondition(apis.ConditionSucceeded)
	if cond == nil || !cond.IsTrue() {
		t.Fatalf("expected Succeeded condition True, got %#v", cond)
	}
	if len(out.Status.Results) != 1 || out.Status.Results[0].Name != "pushed" || out.Status.Results[0].Value != pushed {
		t.Fatalf("unexpected results: %#v", out.Status.Results)
	}
}

func TestReconcileSyncsFailedStatus(t *testing.T) {
	t.Parallel()

	msg := "build exploded"
	cr := newTestMacBuildCustomRun("cr-fail", nil)
	mb := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: cr.Name, Namespace: testMacBuildNamespace},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:   buildv1alpha1.PhaseFailed,
			Message: &msg,
		},
	}

	r, c := newTestCustomRunReconciler(t, cr, mb)
	if _, err := r.Reconcile(context.Background(), customRunRequest(cr)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var out tektonv1beta1.CustomRun
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}, &out); err != nil {
		t.Fatalf("get CustomRun: %v", err)
	}
	cond := out.Status.GetCondition(apis.ConditionSucceeded)
	if cond == nil || !cond.IsFalse() || cond.Message != msg {
		t.Fatalf("expected Failed condition with message %q, got %#v", msg, cond)
	}
}

func TestReconcileRunningRequeues(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-running", nil)
	mb := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: cr.Name, Namespace: testMacBuildNamespace},
		Status:     buildv1alpha1.MacBuildStatus{Phase: buildv1alpha1.PhaseBuilding},
	}

	r, c := newTestCustomRunReconciler(t, cr, mb)
	result, err := r.Reconcile(context.Background(), customRunRequest(cr))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter == 0 {
		t.Fatalf("expected a requeue while the MacBuild is running")
	}

	var out tektonv1beta1.CustomRun
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}, &out); err != nil {
		t.Fatalf("get CustomRun: %v", err)
	}
	cond := out.Status.GetCondition(apis.ConditionSucceeded)
	if cond == nil || !cond.IsUnknown() {
		t.Fatalf("expected Unknown condition while running, got %#v", cond)
	}
}

func TestReconcileMissingCustomRunIsNoop(t *testing.T) {
	t.Parallel()

	cr := newTestMacBuildCustomRun("cr-absent", nil)
	r, _ := newTestCustomRunReconciler(t)
	if _, err := r.Reconcile(context.Background(), customRunRequest(cr)); err != nil {
		t.Fatalf("reconciling a missing CustomRun should be a no-op, got %v", err)
	}
}

func TestReconcilerHelperDefaults(t *testing.T) {
	t.Parallel()

	var zero MacBuildCustomRunReconciler
	if got := zero.macBuildNamespace(); got != "default" {
		t.Fatalf("expected default namespace, got %q", got)
	}
	if got := zero.pollInterval(); got != defaultCustomRunPollInterval {
		t.Fatalf("expected default poll interval, got %s", got)
	}
	if zero.currentTime().IsZero() {
		t.Fatalf("expected currentTime to return a non-zero time")
	}

	fixed := time.Date(2026, 10, 5, 12, 0, 0, 0, time.UTC)
	r := &MacBuildCustomRunReconciler{
		MacBuildNamespace: " ns ",
		PollInterval:      5 * time.Second,
		now:               func() time.Time { return fixed },
	}
	if got := r.macBuildNamespace(); got != " ns " {
		t.Fatalf("expected configured namespace, got %q", got)
	}
	if got := r.pollInterval(); got != 5*time.Second {
		t.Fatalf("expected configured poll interval, got %s", got)
	}
	if got := r.currentTime(); !got.Equal(fixed) {
		t.Fatalf("expected injected time %s, got %s", fixed, got)
	}
}
