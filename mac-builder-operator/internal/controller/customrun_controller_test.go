package controller

import (
	"testing"

	tektonv1beta1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

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
		ObjectMeta: metav1.ObjectMeta{Namespace: "ee-cd", Name: name},
		Spec: tektonv1beta1.CustomRunSpec{
			CustomRef: &tektonv1beta1.TaskRef{APIVersion: macBuildAPIVersion, Kind: macBuildKind, Name: name},
			Params:    items,
		},
	}
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

	mb, err := buildMacBuildFromCustomRun(cr, "mac-builds", "macbuild-1")
	if err != nil {
		t.Fatalf("buildMacBuildFromCustomRun failed: %v", err)
	}

	if mb.Namespace != "mac-builds" || mb.Name != "macbuild-1" {
		t.Fatalf("unexpected object identity: %s/%s", mb.Namespace, mb.Name)
	}
	if mb.Labels[labelCustomRunName] != cr.GetName() || mb.Labels[labelCustomRunNamespace] != "ee-cd" {
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
