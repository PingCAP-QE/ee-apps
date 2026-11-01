package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const testHealthyAgent = "mac-healthy"

func schedulerAgent(name, arch string) buildv1alpha1.MacAgent {
	now := metav1.NewTime(time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC))
	last := metav1.NewTime(now.Add(-10 * time.Second))
	return buildv1alpha1.MacAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:   name,
			Labels: map[string]string{"tibuild.pingcap.net/pool": "mac-mini"},
		},
		Spec: buildv1alpha1.MacAgentSpec{Capacity: 1},
		Status: buildv1alpha1.MacAgentStatus{
			Phase:             buildv1alpha1.MacAgentPhaseOnline,
			Arch:              arch,
			LastHeartbeatTime: &last,
			Runtime:           buildv1alpha1.MacAgentRuntime{DiskFreeBytes: 100 * 1024 * 1024 * 1024, Load1: 0.5},
			Conditions: []metav1.Condition{{
				Type:   buildv1alpha1.MacAgentConditionReady,
				Status: metav1.ConditionTrue,
				Reason: buildv1alpha1.MacAgentReasonHeartbeatOK,
			}},
		},
	}
}

func pendingSchedulerBuild() buildv1alpha1.MacBuild {
	return buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: "macbuild-abc", Namespace: "ee-cd"},
		Spec: buildv1alpha1.MacBuildSpec{
			Build: buildv1alpha1.BuildSpec{Component: "tidb", Version: "nightly", Arch: buildv1alpha1.BuildArchARM64},
		},
		Status: buildv1alpha1.MacBuildStatus{Phase: buildv1alpha1.PhasePending},
	}
}

func newSchedulerClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(macAgentTestScheme(t)).
		WithStatusSubresource(&buildv1alpha1.MacAgent{}, &buildv1alpha1.MacBuild{}).
		WithObjects(objects...).
		Build()
}

func newScheduler(c client.Client, now time.Time) *MacBuildScheduler {
	return &MacBuildScheduler{
		Client:                c,
		MacBuildNamespace:     "ee-cd",
		AssignmentTimeout:     2 * time.Minute,
		RequeueInterval:       30 * time.Second,
		MaxAssignmentAttempts: 3,
		Now:                   func() time.Time { return now },
	}
}

func reconcileScheduler(t *testing.T, c client.Client, now time.Time) {
	t.Helper()
	r := newScheduler(c, now)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
}

func TestSelectAgentFiltersAndScores(t *testing.T) {
	t.Parallel()

	build := pendingSchedulerBuild()
	build.Spec.WorkerSelector = map[string]string{"tibuild.pingcap.net/pool": "mac-mini"}

	healthy := schedulerAgent(testHealthyAgent, buildv1alpha1.BuildArchARM64)
	loaded := schedulerAgent("mac-loaded", buildv1alpha1.BuildArchARM64)
	loaded.Status.Runtime.Load1 = 3.0
	full := schedulerAgent("mac-full", buildv1alpha1.BuildArchARM64)
	archMismatch := schedulerAgent("mac-amd64", buildv1alpha1.BuildArchAMD64)
	cordoned := schedulerAgent("mac-cordoned", buildv1alpha1.BuildArchARM64)
	cordoned.Spec.Unschedulable = true
	drained := schedulerAgent("mac-drained", buildv1alpha1.BuildArchARM64)
	drained.Spec.Drain = true
	diskLow := schedulerAgent("mac-disk-low", buildv1alpha1.BuildArchARM64)
	diskLow.Status.Runtime.DiskFreeBytes = 1
	selectorMismatch := schedulerAgent("mac-pool", buildv1alpha1.BuildArchARM64)
	selectorMismatch.Labels = map[string]string{"tibuild.pingcap.net/pool": "other"}
	notReady := schedulerAgent("mac-not-ready", buildv1alpha1.BuildArchARM64)
	notReady.Status.Phase = buildv1alpha1.MacAgentPhaseOffline
	notReady.Status.Conditions[0].Status = metav1.ConditionFalse

	candidates := []agentCandidate{
		{agent: loaded},
		{agent: healthy},
		{agent: full, active: 1},
		{agent: archMismatch},
		{agent: cordoned},
		{agent: drained},
		{agent: diskLow},
		{agent: selectorMismatch},
		{agent: notReady},
	}
	selected, reason := selectAgent(build, candidates)
	if selected == nil {
		t.Fatalf("expected a candidate, got reason %q", reason)
		return
	}
	if selected.agent.Name != testHealthyAgent {
		t.Fatalf("expected least-loaded healthy agent, got %q", selected.agent.Name)
	}

	selected, reason = selectAgent(build, nil)
	if selected != nil || !strings.Contains(reason, "no MacAgents") {
		t.Fatalf("expected no-agent reason, got %q", reason)
	}

	pressured := schedulerAgent("mac-disk-pressure", buildv1alpha1.BuildArchARM64)
	meta.SetStatusCondition(&pressured.Status.Conditions, metav1.Condition{
		Type:   buildv1alpha1.MacAgentConditionDiskPressure,
		Status: metav1.ConditionTrue,
		Reason: buildv1alpha1.MacAgentReasonDiskPressure,
	})
	selected, reason = selectAgent(build, []agentCandidate{{agent: pressured}})
	if selected != nil || !strings.Contains(reason, "disk") {
		t.Fatalf("a DiskPressure agent must not be selected, got %v / %q", selected, reason)
	}
}

func TestSchedulerAssignsPendingBuild(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := schedulerAgent(testHealthyAgent, buildv1alpha1.BuildArchARM64)
	build := pendingSchedulerBuild()
	c := newSchedulerClient(t, &agent, &build)
	reconcileScheduler(t, c, now)

	var got buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}, &got); err != nil {
		t.Fatalf("get build: %v", err)
	}
	if got.Status.Phase != buildv1alpha1.PhaseAssigned {
		t.Fatalf("unexpected phase: %q", got.Status.Phase)
	}
	if got.Status.Assignment == nil || got.Status.Assignment.AgentName != testHealthyAgent {
		t.Fatalf("unexpected assignment: %#v", got.Status.Assignment)
	}
	if got.Status.Assignment.Attempt != 1 || got.Status.AssignmentAttempts != 0 {
		t.Fatalf("unexpected assignment attempts: %#v", got.Status.Assignment)
	}
	if got.Status.Assignment.LeaseDeadline == nil || !got.Status.Assignment.LeaseDeadline.Time.Equal(now.Add(2*time.Minute)) {
		t.Fatalf("unexpected lease deadline: %#v", got.Status.Assignment.LeaseDeadline)
	}
	if got.Status.WorkerID == nil || *got.Status.WorkerID != testHealthyAgent {
		t.Fatalf("unexpected workerID: %#v", got.Status.WorkerID)
	}
	if got.Status.Logs == nil || got.Status.Logs.Command != "kubectl macbuild logs -n ee-cd macbuild-abc" {
		t.Fatalf("unexpected logs command: %#v", got.Status.Logs)
	}
	if len(got.Status.PhaseHistory) == 0 || got.Status.PhaseHistory[len(got.Status.PhaseHistory)-1].Phase != buildv1alpha1.PhaseAssigned {
		t.Fatalf("unexpected phase history: %#v", got.Status.PhaseHistory)
	}

	var gotAgent buildv1alpha1.MacAgent
	if err := c.Get(context.Background(), types.NamespacedName{Name: testHealthyAgent}, &gotAgent); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if gotAgent.Status.LastAssignedAt == nil || !gotAgent.Status.LastAssignedAt.Time.Equal(now) {
		t.Fatalf("unexpected lastAssignedAt: %#v", gotAgent.Status.LastAssignedAt)
	}
}

func TestSchedulerLeavesBuildPendingWithoutAgent(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	build := pendingSchedulerBuild()
	c := newSchedulerClient(t, &build)
	reconcileScheduler(t, c, now)

	var got buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}, &got); err != nil {
		t.Fatalf("get build: %v", err)
	}
	if got.Status.Phase != buildv1alpha1.PhasePending || got.Status.Assignment != nil {
		t.Fatalf("expected pending build, got %#v", got.Status)
	}
	if got.Status.PhaseMessage == nil || !strings.Contains(*got.Status.PhaseMessage, "no MacAgents") {
		t.Fatalf("unexpected phase message: %#v", got.Status.PhaseMessage)
	}
}

func TestSchedulerReassignsExpiredAssignment(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := schedulerAgent(testHealthyAgent, buildv1alpha1.BuildArchARM64)
	build := pendingSchedulerBuild()
	expired := metav1.NewTime(now.Add(-time.Minute))
	build.Status.Phase = buildv1alpha1.PhaseAssigned
	build.Status.Assignment = &buildv1alpha1.MacBuildAssignment{
		AgentName:     testHealthyAgent,
		AssignedAt:    metav1.NewTime(now.Add(-3 * time.Minute)),
		LeaseDeadline: &expired,
		Attempt:       1,
	}
	c := newSchedulerClient(t, &agent, &build)
	reconcileScheduler(t, c, now)

	var got buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}, &got); err != nil {
		t.Fatalf("get build: %v", err)
	}
	if got.Status.Phase != buildv1alpha1.PhasePending {
		t.Fatalf("expected pending after lease timeout, got %q", got.Status.Phase)
	}
	if got.Status.Assignment != nil {
		t.Fatalf("expected assignment cleared, got %#v", got.Status.Assignment)
	}
	if got.Status.AssignmentAttempts != 1 {
		t.Fatalf("expected assignmentAttempts 1, got %d", got.Status.AssignmentAttempts)
	}
}

func TestSchedulerFailsAfterMaxAttempts(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := schedulerAgent(testHealthyAgent, buildv1alpha1.BuildArchARM64)
	build := pendingSchedulerBuild()
	expired := metav1.NewTime(now.Add(-time.Minute))
	build.Status.Phase = buildv1alpha1.PhaseAssigned
	build.Status.AssignmentAttempts = 2
	build.Status.Assignment = &buildv1alpha1.MacBuildAssignment{
		AgentName:     testHealthyAgent,
		AssignedAt:    metav1.NewTime(now.Add(-3 * time.Minute)),
		LeaseDeadline: &expired,
		Attempt:       3,
	}
	c := newSchedulerClient(t, &agent, &build)
	reconcileScheduler(t, c, now)

	var got buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}, &got); err != nil {
		t.Fatalf("get build: %v", err)
	}
	if got.Status.Phase != buildv1alpha1.PhaseFailed {
		t.Fatalf("expected failed after max attempts, got %q", got.Status.Phase)
	}
	if got.Status.Message == nil || !strings.Contains(*got.Status.Message, "did not start") {
		t.Fatalf("unexpected failure message: %#v", got.Status.Message)
	}
}

func TestSchedulerIgnoresActiveBuild(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	agent := schedulerAgent(testHealthyAgent, buildv1alpha1.BuildArchARM64)
	build := pendingSchedulerBuild()
	build.Status.Phase = buildv1alpha1.PhaseBuilding
	c := newSchedulerClient(t, &agent, &build)
	reconcileScheduler(t, c, now)

	var got buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ee-cd", Name: "macbuild-abc"}, &got); err != nil {
		t.Fatalf("get build: %v", err)
	}
	if got.Status.Phase != buildv1alpha1.PhaseBuilding || got.Status.Assignment != nil {
		t.Fatalf("active build was modified: %#v", got.Status)
	}
}

func BenchmarkSelectAgent500Builds(b *testing.B) {
	candidates := make([]agentCandidate, 50)
	for i := range candidates {
		candidates[i] = agentCandidate{agent: schedulerAgent(fmt.Sprintf("mac-%02d", i), buildv1alpha1.BuildArchARM64)}
	}
	builds := make([]buildv1alpha1.MacBuild, 500)
	for i := range builds {
		builds[i] = pendingSchedulerBuild()
		builds[i].Name = fmt.Sprintf("build-%03d", i)
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		for j := range builds {
			if _, reason := selectAgent(builds[j], candidates); reason != "" {
				b.Fatalf("unexpected reason: %s", reason)
			}
		}
	}
}
