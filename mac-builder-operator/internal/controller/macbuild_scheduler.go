package controller

import (
	"context"
	"fmt"
	"sort"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	defaultAssignmentTimeout     = 2 * time.Minute
	defaultSchedulerRequeue      = 30 * time.Second
	defaultMaxAssignmentAttempts = 3
	macBuildAgentLabel           = "tibuild.pingcap.net/agent"
)

// MacBuildScheduler assigns Pending MacBuilds to matching MacAgents and reaps
// assignments whose lease expires before the agent starts the build.
type MacBuildScheduler struct {
	client.Client
	APIReader client.Reader
	Scheme    *runtime.Scheme

	Recorder record.EventRecorder

	MacBuildNamespace     string
	AssignmentTimeout     time.Duration
	RequeueInterval       time.Duration
	MaxAssignmentAttempts int32
	Now                   func() time.Time
}

type agentCandidate struct {
	agent  buildv1alpha1.MacAgent
	active int32
}

// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macagents,verbs=get;list;watch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile schedules one Pending build or reaps one Assigned build.
func (r *MacBuildScheduler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var build buildv1alpha1.MacBuild
	if err := r.reader().Get(ctx, req.NamespacedName, &build); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	now := r.now()
	switch build.Status.Phase {
	case "":
		return r.initializePending(ctx, &build, now)
	case buildv1alpha1.PhasePending:
		if build.Status.Assignment != nil {
			return r.clearStaleAssignment(ctx, &build, now)
		}
		return r.schedule(ctx, &build, now)
	case buildv1alpha1.PhaseAssigned:
		return r.reapAssignment(ctx, &build, now)
	default:
		return ctrl.Result{}, nil
	}
}

// SetupWithManager registers the scheduler and wakes it when an agent changes.
func (r *MacBuildScheduler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Recorder == nil {
		r.Recorder = mgr.GetEventRecorderFor("macbuild-scheduler")
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&buildv1alpha1.MacBuild{}).
		Watches(&buildv1alpha1.MacAgent{}, handler.EnqueueRequestsFromMapFunc(r.agentToPendingBuilds)).
		Named("macbuild-scheduler").
		Complete(r)
}

func (r *MacBuildScheduler) schedule(ctx context.Context, build *buildv1alpha1.MacBuild, now time.Time) (ctrl.Result, error) {
	candidates, err := r.candidates(ctx)
	if err != nil {
		return ctrl.Result{}, err
	}
	selected, reason := selectAgent(*build, candidates)
	if selected == nil {
		if err := r.setPendingMessage(ctx, build, reason); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
	}

	if err := r.assign(ctx, build, selected.agent, now); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
}

func (r *MacBuildScheduler) candidates(ctx context.Context) ([]agentCandidate, error) {
	var agents buildv1alpha1.MacAgentList
	if err := r.List(ctx, &agents); err != nil {
		return nil, fmt.Errorf("list MacAgents: %w", err)
	}
	active, err := r.activeByAgent(ctx)
	if err != nil {
		return nil, err
	}
	candidates := make([]agentCandidate, 0, len(agents.Items))
	for _, agent := range agents.Items {
		candidates = append(candidates, agentCandidate{agent: agent, active: active[agent.Name]})
	}
	return candidates, nil
}

func (r *MacBuildScheduler) activeByAgent(ctx context.Context) (map[string]int32, error) {
	builds, err := r.listBuilds(ctx)
	if err != nil {
		return nil, err
	}
	active := make(map[string]int32)
	for _, build := range builds {
		if build.Status.Assignment == nil || !isActiveBuildPhase(build.Status.Phase) {
			continue
		}
		active[build.Status.Assignment.AgentName]++
	}
	return active, nil
}

func (r *MacBuildScheduler) listBuilds(ctx context.Context) ([]buildv1alpha1.MacBuild, error) {
	var list buildv1alpha1.MacBuildList
	opts := []client.ListOption{}
	if r.MacBuildNamespace != "" {
		opts = append(opts, client.InNamespace(r.MacBuildNamespace))
	}
	if err := r.List(ctx, &list, opts...); err != nil {
		return nil, fmt.Errorf("list MacBuilds: %w", err)
	}
	return list.Items, nil
}

func (r *MacBuildScheduler) initializePending(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	now time.Time,
) (ctrl.Result, error) {
	message := "Waiting for a matching MacAgent."
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if latest.Status.Phase != "" {
			return nil
		}
		base := latest.DeepCopy()
		latest.Status.SetPhase(buildv1alpha1.PhasePending, message, metav1.NewTime(now))
		latest.Status.PhaseMessage = &message
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("initialize Pending phase: %w", err)
	}
	return ctrl.Result{Requeue: true}, nil
}

func (r *MacBuildScheduler) setPendingMessage(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	reason string,
) error {
	macBuildAssignments.WithLabelValues("no_candidate").Inc()
	message := "Waiting for a matching MacAgent: " + reason
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if latest.Status.Phase != buildv1alpha1.PhasePending || latest.Status.Assignment != nil {
			return nil
		}
		if latest.Status.PhaseMessage != nil && *latest.Status.PhaseMessage == message {
			return nil
		}
		base := latest.DeepCopy()
		latest.Status.PhaseMessage = &message
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
}

func (r *MacBuildScheduler) assign(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	agent buildv1alpha1.MacAgent,
	now time.Time,
) error {
	key := client.ObjectKeyFromObject(build)
	assigned := false

	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, key, &latest); err != nil {
			return err
		}
		if latest.Status.Phase != buildv1alpha1.PhasePending || latest.Status.Assignment != nil {
			return nil
		}

		base := latest.DeepCopy()
		deadline := metav1.NewTime(now.Add(r.assignmentTimeout()))
		assignedAt := metav1.NewTime(now)
		message := fmt.Sprintf("Assigned to %s; waiting for the agent to start.", agent.Name)
		attempt := latest.Status.AssignmentAttempts + 1

		latest.Status.SetPhase(buildv1alpha1.PhaseAssigned, message, assignedAt)
		latest.Status.Assignment = &buildv1alpha1.MacBuildAssignment{
			AgentName:     agent.Name,
			AgentUID:      agent.UID,
			AssignedAt:    assignedAt,
			LeaseDeadline: &deadline,
			Attempt:       attempt,
		}
		latest.Status.WorkerID = &agent.Name
		arch := agent.Status.Arch
		if arch == "" {
			arch = buildArchFor(latest)
		}
		latest.Status.WorkerArch = optionalString(arch)
		latest.Status.Message = nil
		latest.Status.CompletionTime = nil
		if latest.Status.Logs == nil {
			latest.Status.Logs = &buildv1alpha1.MacBuildLogs{}
		}

		if err := r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return err
		}
		assigned = true
		return nil
	}); err != nil {
		return fmt.Errorf("assign build: %w", err)
	}
	if !assigned {
		return nil
	}
	macBuildAssignments.WithLabelValues("assigned").Inc()

	if err := r.patchAgentLabel(ctx, key, agent.Name); err != nil {
		logf.FromContext(ctx).Error(err, "Failed to label MacBuild with its agent; continuing")
	}
	if err := r.updateAgentLastAssigned(ctx, agent.Name, now); err != nil {
		logf.FromContext(ctx).Error(err, "Failed to update MacAgent lastAssignedAt; continuing")
	}
	if r.Recorder != nil {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, key, &latest); err == nil {
			r.Recorder.Event(&latest, "Normal", "Scheduled", fmt.Sprintf("Assigned to %s", agent.Name))
		}
	}
	return nil
}

func (r *MacBuildScheduler) reapAssignment(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	now time.Time,
) (ctrl.Result, error) {
	assignment := build.Status.Assignment
	if assignment == nil || assignment.LeaseDeadline == nil {
		return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
	}
	if now.Before(assignment.LeaseDeadline.Time) {
		return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
	}

	agentName := assignment.AgentName
	attempts := build.Status.AssignmentAttempts + 1
	failed := attempts >= r.maxAssignmentAttempts()
	timeout := r.assignmentTimeout()

	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if latest.Status.Phase != buildv1alpha1.PhaseAssigned || latest.Status.Assignment == nil {
			return nil
		}
		if latest.Status.Assignment.LeaseDeadline != nil && now.Before(latest.Status.Assignment.LeaseDeadline.Time) {
			return nil
		}

		base := latest.DeepCopy()
		latest.Status.AssignmentAttempts = attempts
		if failed {
			message := fmt.Sprintf(
				"agent %s did not start the build within %s (attempt %d/%d); giving up",
				agentName, humanDuration(timeout), attempts, r.maxAssignmentAttempts(),
			)
			latest.Status.SetPhase(buildv1alpha1.PhaseFailed, message, metav1.NewTime(now))
			latest.Status.Message = &message
			completion := metav1.NewTime(now)
			latest.Status.CompletionTime = &completion
		} else {
			message := fmt.Sprintf(
				"agent %s did not start the build within %s (attempt %d/%d); reassigning",
				agentName, humanDuration(timeout), attempts, r.maxAssignmentAttempts(),
			)
			latest.Status.SetPhase(buildv1alpha1.PhasePending, message, metav1.NewTime(now))
			latest.Status.Assignment = nil
			latest.Status.WorkerID = nil
			latest.Status.WorkerArch = nil
			latest.Status.Message = nil
			latest.Status.CompletionTime = nil
		}
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("reap assignment: %w", err)
	}

	result := "reassigned"
	if failed {
		result = "failed"
	}
	macBuildAssignments.WithLabelValues(result).Inc()

	if r.Recorder != nil {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, client.ObjectKeyFromObject(build), &latest); err == nil {
			reason := "AssignmentTimeout"
			if failed {
				reason = "AssignmentFailed"
			}
			r.Recorder.Event(&latest, "Warning", reason, *latest.Status.PhaseMessage)
		}
	}
	return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
}

func (r *MacBuildScheduler) clearStaleAssignment(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	now time.Time,
) (ctrl.Result, error) {
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if latest.Status.Phase != buildv1alpha1.PhasePending || latest.Status.Assignment == nil {
			return nil
		}
		base := latest.DeepCopy()
		latest.Status.Assignment = nil
		latest.Status.WorkerID = nil
		latest.Status.WorkerArch = nil
		message := "Cleared a stale assignment; waiting for a matching MacAgent."
		latest.Status.PhaseMessage = &message
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("clear stale assignment: %w", err)
	}
	_ = now
	return ctrl.Result{Requeue: true}, nil
}

func (r *MacBuildScheduler) patchAgentLabel(ctx context.Context, key client.ObjectKey, agentName string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.reader().Get(ctx, key, &latest); err != nil {
			return err
		}
		if latest.Labels[macBuildAgentLabel] == agentName {
			return nil
		}
		base := latest.DeepCopy()
		if latest.Labels == nil {
			latest.Labels = map[string]string{}
		}
		latest.Labels[macBuildAgentLabel] = agentName
		return r.Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
}

func (r *MacBuildScheduler) updateAgentLastAssigned(ctx context.Context, agentName string, now time.Time) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var agent buildv1alpha1.MacAgent
		if err := r.reader().Get(ctx, types.NamespacedName{Name: agentName}, &agent); err != nil {
			return client.IgnoreNotFound(err)
		}
		base := agent.DeepCopy()
		assignedAt := metav1.NewTime(now)
		agent.Status.LastAssignedAt = &assignedAt
		return r.Status().Patch(ctx, &agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
}

func (r *MacBuildScheduler) agentToPendingBuilds(ctx context.Context, _ client.Object) []reconcile.Request {
	builds, err := r.listBuilds(ctx)
	if err != nil {
		logf.FromContext(ctx).Error(err, "Failed to list pending MacBuilds for MacAgent event")
		return nil
	}
	requests := make([]reconcile.Request, 0, len(builds))
	for _, build := range builds {
		if build.Status.Phase != buildv1alpha1.PhasePending || build.Status.Assignment != nil {
			continue
		}
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&build)})
	}
	return requests
}

func selectAgent(build buildv1alpha1.MacBuild, candidates []agentCandidate) (*agentCandidate, string) {
	if len(candidates) == 0 {
		return nil, "no MacAgents registered"
	}

	matching := make([]agentCandidate, 0, len(candidates))
	for _, candidate := range candidates {
		if !agentMatchesBuild(candidate, build) {
			continue
		}
		matching = append(matching, candidate)
	}
	if len(matching) == 0 {
		return nil, "no schedulable MacAgent (readiness, architecture, selector, capacity or disk)"
	}

	sort.SliceStable(matching, func(i, j int) bool {
		return agentCandidateLess(matching[i], matching[j])
	})
	selected := matching[0]
	return &selected, ""
}

func agentMatchesBuild(candidate agentCandidate, build buildv1alpha1.MacBuild) bool {
	agent := candidate.agent
	if agent.Status.Phase != buildv1alpha1.MacAgentPhaseOnline {
		return false
	}
	ready := meta.FindStatusCondition(agent.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	if ready == nil || ready.Status != metav1.ConditionTrue {
		return false
	}
	if agent.Spec.Unschedulable || agent.Spec.Drain {
		return false
	}
	if candidate.active >= agent.Spec.CapacityOrDefault() {
		return false
	}
	if buildArchFor(build) != buildv1alpha1.NormalizeBuildArch(agent.Status.Arch) {
		return false
	}
	for key, value := range build.Spec.WorkerSelector {
		if agent.Labels[key] != value {
			return false
		}
	}
	if diskPressure(&agent.Status) {
		return false
	}
	if agent.Status.Runtime.DiskFreeBytes < agent.Spec.MinFreeDiskBytesOrDefault() {
		return false
	}
	return true
}

func agentCandidateLess(a, b agentCandidate) bool {
	aUtilization := float64(a.active) / float64(a.agent.Spec.CapacityOrDefault())
	bUtilization := float64(b.active) / float64(b.agent.Spec.CapacityOrDefault())
	if aUtilization != bUtilization {
		return aUtilization < bUtilization
	}

	aLoad := normalizedLoad(a.agent)
	bLoad := normalizedLoad(b.agent)
	if aLoad != bLoad {
		return aLoad < bLoad
	}

	aAssigned := time.Time{}
	if a.agent.Status.LastAssignedAt != nil {
		aAssigned = a.agent.Status.LastAssignedAt.Time
	}
	bAssigned := time.Time{}
	if b.agent.Status.LastAssignedAt != nil {
		bAssigned = b.agent.Status.LastAssignedAt.Time
	}
	if !aAssigned.Equal(bAssigned) {
		return aAssigned.Before(bAssigned)
	}
	return a.agent.Name < b.agent.Name
}

func normalizedLoad(agent buildv1alpha1.MacAgent) float64 {
	cores := agent.Status.Hardware.CPUCores
	if cores <= 0 {
		return 0
	}
	return agent.Status.Runtime.Load1 / float64(cores)
}

func isActiveBuildPhase(phase string) bool {
	switch phase {
	case buildv1alpha1.PhaseAssigned, buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding, buildv1alpha1.PhasePublishing:
		return true
	default:
		return false
	}
}

func (r *MacBuildScheduler) reader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

func (r *MacBuildScheduler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *MacBuildScheduler) assignmentTimeout() time.Duration {
	if r.AssignmentTimeout > 0 {
		return r.AssignmentTimeout
	}
	return defaultAssignmentTimeout
}

func (r *MacBuildScheduler) requeueInterval() time.Duration {
	if r.RequeueInterval > 0 {
		return r.RequeueInterval
	}
	return defaultSchedulerRequeue
}

func (r *MacBuildScheduler) maxAssignmentAttempts() int32 {
	if r.MaxAssignmentAttempts > 0 {
		return r.MaxAssignmentAttempts
	}
	return defaultMaxAssignmentAttempts
}
