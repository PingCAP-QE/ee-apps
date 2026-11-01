package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

func macAgentTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	return scheme
}

func newMacAgentTestClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(macAgentTestScheme(t)).
		WithStatusSubresource(&buildv1alpha1.MacAgent{}, &buildv1alpha1.MacBuild{}).
		WithObjects(objects...).
		Build()
}

func baseAgent(now time.Time, name string) *buildv1alpha1.MacAgent {
	connected := metav1.NewTime(now.Add(-3*time.Hour - 12*time.Minute))
	heartbeat := metav1.NewTime(now.Add(-10 * time.Second))
	return &buildv1alpha1.MacAgent{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       buildv1alpha1.MacAgentSpec{Capacity: 1},
		Status: buildv1alpha1.MacAgentStatus{
			ConnectedSince:    &connected,
			LastHeartbeatTime: &heartbeat,
			Hostname:          "mac-mini-01",
			IP:                "10.0.0.5",
			Arch:              buildv1alpha1.BuildArchARM64,
			Hardware:          buildv1alpha1.MacAgentHardware{CPUCores: 8, MemoryBytes: 16 * 1024 * 1024 * 1024},
			Runtime:           buildv1alpha1.MacAgentRuntime{Load1: 1.25, Load5: 1.1, Load15: 0.95, DiskFreeBytes: 232 * 1024 * 1024 * 1024},
		},
	}
}

func assignedBuild(agentName string) *buildv1alpha1.MacBuild {
	return &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: "macbuild-abc", Namespace: "ee-cd"},
		Status: buildv1alpha1.MacBuildStatus{
			Phase: buildv1alpha1.PhaseBuilding,
			Assignment: &buildv1alpha1.MacBuildAssignment{
				AgentName:  agentName,
				AssignedAt: metav1.Now(),
			},
		},
	}
}

func reconcileMacAgent(t *testing.T, c client.Client, now time.Time, name string) buildv1alpha1.MacAgent {
	t.Helper()
	r := &MacAgentReconciler{
		Client:            c,
		MacBuildNamespace: "ee-cd",
		OfflineThreshold:  60 * time.Second,
		Now:               func() time.Time { return now },
	}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: name}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got buildv1alpha1.MacAgent
	if err := c.Get(context.Background(), types.NamespacedName{Name: name}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	return got
}

func TestMacAgentControllerOnlineSummary(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	c := newMacAgentTestClient(t, agent, assignedBuild("mac-test"))
	got := reconcileMacAgent(t, c, now, "mac-test")

	if got.Status.Phase != buildv1alpha1.MacAgentPhaseOnline {
		t.Fatalf("unexpected phase: %q", got.Status.Phase)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	if ready == nil || ready.Status != metav1.ConditionTrue || ready.Reason != buildv1alpha1.MacAgentReasonHeartbeatOK {
		t.Fatalf("unexpected Ready condition: %#v", ready)
	}
	schedulable := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionSchedulable)
	if schedulable == nil || schedulable.Status != metav1.ConditionFalse || schedulable.Reason != buildv1alpha1.MacAgentReasonCapacityFull {
		t.Fatalf("unexpected Schedulable condition: %#v", schedulable)
	}
	if got.Status.ActiveBuilds != 1 || len(got.Status.AssignedBuilds) != 1 {
		t.Fatalf("unexpected active builds: %#v", got.Status)
	}
	if got.Status.OnlineDuration != "3h12m" {
		t.Fatalf("unexpected online duration: %q", got.Status.OnlineDuration)
	}
	if !strings.Contains(got.Status.Summary, "Online 3h12m") || !strings.Contains(got.Status.Summary, "build ee-cd/macbuild-abc (1/1)") {
		t.Fatalf("unexpected summary: %q", got.Status.Summary)
	}
	if got.Status.Summary != "Online 3h12m | 10.0.0.5 | arm64 | 8C/16GiB | load 1.25/1.10/0.95 | disk 232Gi free | build ee-cd/macbuild-abc (1/1)" {
		t.Fatalf("unexpected summary: %q", got.Status.Summary)
	}
}

func TestMacAgentControllerOffline(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	last := metav1.NewTime(now.Add(-2 * time.Minute))
	agent.Status.LastHeartbeatTime = &last
	c := newMacAgentTestClient(t, agent, assignedBuild("mac-test"))
	got := reconcileMacAgent(t, c, now, "mac-test")

	if got.Status.Phase != buildv1alpha1.MacAgentPhaseOffline {
		t.Fatalf("unexpected phase: %q", got.Status.Phase)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	if ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != buildv1alpha1.MacAgentReasonHeartbeatLost {
		t.Fatalf("unexpected Ready condition: %#v", ready)
	}
	if got.Status.OnlineDuration != "3h10m" {
		t.Fatalf("unexpected frozen online duration: %q", got.Status.OnlineDuration)
	}
	if !strings.Contains(got.Status.Summary, "Offline") || !strings.Contains(got.Status.Summary, "last heartbeat 2m ago") {
		t.Fatalf("unexpected offline summary: %q", got.Status.Summary)
	}
}

func TestMacAgentControllerCordonedAndDraining(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)

	cordoned := baseAgent(now, "mac-cordoned")
	cordoned.Spec.Unschedulable = true
	c := newMacAgentTestClient(t, cordoned)
	got := reconcileMacAgent(t, c, now, "mac-cordoned")
	if got.Status.Phase != buildv1alpha1.MacAgentPhaseCordoned {
		t.Fatalf("unexpected cordoned phase: %q", got.Status.Phase)
	}
	schedulable := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionSchedulable)
	if schedulable == nil || schedulable.Reason != buildv1alpha1.MacAgentReasonCordoned {
		t.Fatalf("unexpected cordoned Schedulable condition: %#v", schedulable)
	}

	draining := baseAgent(now, "mac-draining")
	draining.Spec.Drain = true
	c = newMacAgentTestClient(t, draining, assignedBuild("mac-draining"))
	got = reconcileMacAgent(t, c, now, "mac-draining")
	if got.Status.Phase != buildv1alpha1.MacAgentPhaseDraining {
		t.Fatalf("unexpected draining phase: %q", got.Status.Phase)
	}

	drained := baseAgent(now, "mac-drained")
	drained.Spec.Drain = true
	c = newMacAgentTestClient(t, drained)
	got = reconcileMacAgent(t, c, now, "mac-drained")
	if got.Status.Phase != buildv1alpha1.MacAgentPhaseDrained {
		t.Fatalf("unexpected drained phase: %q", got.Status.Phase)
	}
}

func TestMacAgentControllerDiskPressure(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	agent.Status.Conditions = []metav1.Condition{{
		Type:               buildv1alpha1.MacAgentConditionDiskPressure,
		Status:             metav1.ConditionTrue,
		Reason:             buildv1alpha1.MacAgentReasonDiskPressure,
		LastTransitionTime: metav1.NewTime(now),
	}}
	agent.Status.Runtime.DiskFreeBytes = 1
	c := newMacAgentTestClient(t, agent)
	got := reconcileMacAgent(t, c, now, "mac-test")

	schedulable := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionSchedulable)
	if schedulable == nil || schedulable.Status != metav1.ConditionFalse || schedulable.Reason != buildv1alpha1.MacAgentReasonDiskPressure {
		t.Fatalf("unexpected disk-pressure Schedulable condition: %#v", schedulable)
	}
	disk := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if disk == nil || disk.Status != metav1.ConditionTrue {
		t.Fatalf("agent-owned DiskPressure condition was overwritten: %#v", disk)
	}
}

func TestMacAgentControllerNoHeartbeat(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	agent.Status.LastHeartbeatTime = nil
	c := newMacAgentTestClient(t, agent)
	got := reconcileMacAgent(t, c, now, "mac-test")

	if got.Status.Phase != buildv1alpha1.MacAgentPhaseUnknown {
		t.Fatalf("unexpected phase: %q", got.Status.Phase)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	if ready == nil || ready.Reason != buildv1alpha1.MacAgentReasonNoHeartbeat {
		t.Fatalf("unexpected Ready condition: %#v", ready)
	}
}

func fetchBuild(t *testing.T, c client.Client, name string) *buildv1alpha1.MacBuild {
	t.Helper()
	var build buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: name}, &build); err != nil {
		t.Fatalf("get build: %v", err)
	}
	return &build
}

func TestMacAgentControllerKeepsBuildDuringGrace(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	last := metav1.NewTime(now.Add(-2 * time.Minute))
	agent.Status.LastHeartbeatTime = &last
	c := newMacAgentTestClient(t, agent, assignedBuild("mac-test"))
	reconcileMacAgent(t, c, now, "mac-test")

	build := fetchBuild(t, c, "macbuild-abc")
	if build.Status.Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("build phase changed during grace: %q", build.Status.Phase)
	}
	if build.Status.PhaseMessage == nil || !strings.Contains(*build.Status.PhaseMessage, "will fail at") {
		t.Fatalf("unexpected grace message: %#v", build.Status.PhaseMessage)
	}
}

func TestMacAgentControllerFailsBuildAfterGrace(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	last := metav1.NewTime(now.Add(-11 * time.Minute))
	agent.Status.LastHeartbeatTime = &last
	c := newMacAgentTestClient(t, agent, assignedBuild("mac-test"))
	reconcileMacAgent(t, c, now, "mac-test")

	build := fetchBuild(t, c, "macbuild-abc")
	if build.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected build to fail after grace, got %q", build.Status.Phase)
	}
	if build.Status.Message == nil || !strings.Contains(*build.Status.Message, "stopped reporting") {
		t.Fatalf("unexpected AgentLost message: %#v", build.Status.Message)
	}
	if build.Status.CompletionTime == nil {
		t.Fatal("expected completion time on AgentLost failure")
	}
}

func TestMacAgentControllerReconnectClearsGraceMessage(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := baseAgent(now, "mac-test")
	build := assignedBuild("mac-test")
	build.Status.PhaseMessage = stringPtr("agent mac-test unreachable since 2026-11-01T11:50:00Z; build will fail at 2026-11-01T12:00:00Z unless the agent reconnects")
	c := newMacAgentTestClient(t, agent, build)
	reconcileMacAgent(t, c, now, "mac-test")

	updated := fetchBuild(t, c, "macbuild-abc")
	if updated.Status.Phase != buildv1alpha1.PhaseBuilding {
		t.Fatalf("build phase changed on reconnect: %q", updated.Status.Phase)
	}
	if updated.Status.PhaseMessage == nil || !strings.Contains(*updated.Status.PhaseMessage, "reconnected") {
		t.Fatalf("unexpected reconnect message: %#v", updated.Status.PhaseMessage)
	}
}
