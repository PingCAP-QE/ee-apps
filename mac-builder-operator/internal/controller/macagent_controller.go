package controller

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	defaultAgentOfflineThreshold = 60 * time.Second
	defaultAgentRequeueInterval  = 30 * time.Second
)

// MacAgentReconciler maintains the cluster-side view of a macOS worker:
// heartbeat readiness, schedulability, phase, summary and assigned builds.
type MacAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	Recorder record.EventRecorder

	MacBuildNamespace string
	OfflineThreshold  time.Duration
	RequeueInterval   time.Duration
	Now               func() time.Time
}

// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macagents,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile refreshes one MacAgent's cluster-side status.
func (r *MacAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)

	var agent buildv1alpha1.MacAgent
	if err := r.Get(ctx, req.NamespacedName, &agent); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	builds, err := r.assignedBuilds(ctx, agent.Name)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("list assigned MacBuilds: %w", err)
	}

	now := r.now()
	oldPhase := agent.Status.Phase
	oldReady := meta.FindStatusCondition(agent.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	ready := false

	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacAgent
		if err := r.Get(ctx, req.NamespacedName, &latest); err != nil {
			return err
		}
		base := latest.DeepCopy()
		ready = r.applyStatus(&latest, builds, now)
		if statusEqual(base.Status, latest.Status) {
			return nil
		}
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	}); err != nil {
		return ctrl.Result{}, fmt.Errorf("patch MacAgent status: %w", err)
	}

	if err := r.Get(ctx, req.NamespacedName, &agent); err == nil {
		r.recordEvents(&agent, oldPhase, oldReady)
		if err := r.reconcileBuilds(ctx, &agent, builds, now, ready); err != nil {
			return ctrl.Result{}, err
		}
		logger.V(1).Info("refreshed MacAgent", "phase", agent.Status.Phase, "activeBuilds", agent.Status.ActiveBuilds)
	}

	return ctrl.Result{RequeueAfter: r.requeueInterval()}, nil
}

// SetupWithManager registers the controller.
func (r *MacAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Recorder == nil {
		r.Recorder = mgr.GetEventRecorderFor("macagent-controller")
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&buildv1alpha1.MacAgent{}).
		Named("macagent").
		Complete(r)
}

func (r *MacAgentReconciler) applyStatus(agent *buildv1alpha1.MacAgent, builds []buildv1alpha1.MacBuild, now time.Time) bool {
	status := &agent.Status
	capacity := agent.Spec.CapacityOrDefault()
	active := int32(0)
	completed := int64(0)
	failed := int64(0)
	assigned := make([]buildv1alpha1.MacBuildReference, 0, len(builds))
	names := make([]string, 0, len(builds))

	for _, build := range builds {
		switch build.Status.Phase {
		case buildv1alpha1.PhaseAssigned, buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding, buildv1alpha1.PhasePublishing:
			active++
			assigned = append(assigned, buildv1alpha1.MacBuildReference{Namespace: build.Namespace, Name: build.Name})
			names = append(names, build.Namespace+"/"+build.Name)
		case buildv1alpha1.PhaseSucceeded:
			completed++
		case buildv1alpha1.PhaseFailed:
			failed++
		}
	}
	sort.Slice(assigned, func(i, j int) bool {
		if assigned[i].Namespace != assigned[j].Namespace {
			return assigned[i].Namespace < assigned[j].Namespace
		}
		return assigned[i].Name < assigned[j].Name
	})
	sort.Strings(names)

	status.ActiveBuilds = active
	status.AssignedBuilds = assigned
	status.Stats = buildv1alpha1.MacAgentStats{Completed: completed, Failed: failed}

	phase, ready, heartbeatAge := r.evaluate(agent, active, now)
	status.Phase = phase
	status.OnlineDuration = onlineDuration(status.ConnectedSince, status.LastHeartbeatTime, ready, now)
	summaryHeartbeatAge := ""
	if !ready {
		summaryHeartbeatAge = heartbeatAge
	}
	status.Summary = formatMacAgentSummary(macAgentSummaryInput{
		Phase:            phase,
		OnlineDuration:   status.OnlineDuration,
		IP:               status.IP,
		Arch:             status.Arch,
		CPUCores:         status.Hardware.CPUCores,
		MemoryBytes:      status.Hardware.MemoryBytes,
		Load1:            status.Runtime.Load1,
		Load5:            status.Runtime.Load5,
		Load15:           status.Runtime.Load15,
		DiskFreeBytes:    status.Runtime.DiskFreeBytes,
		ActiveBuilds:     active,
		Capacity:         capacity,
		Builds:           names,
		LastHeartbeatAge: summaryHeartbeatAge,
	})

	r.setReadyCondition(status, agent, ready, heartbeatAge, now)
	r.setSchedulableCondition(status, agent, ready, active, capacity)
	r.setDrainingCondition(status, agent, active)
	return ready
}

func (r *MacAgentReconciler) evaluate(
	agent *buildv1alpha1.MacAgent,
	active int32,
	now time.Time,
) (phase string, ready bool, heartbeatAge string) {
	status := &agent.Status
	if status.LastHeartbeatTime == nil {
		return buildv1alpha1.MacAgentPhaseUnknown, false, ""
	}

	last := status.LastHeartbeatTime.Time
	if last.After(now) {
		last = now
	}
	age := now.Sub(last)
	heartbeatAge = humanDuration(age)
	if age > r.offlineThreshold() {
		return buildv1alpha1.MacAgentPhaseOffline, false, heartbeatAge
	}

	phase = buildv1alpha1.MacAgentPhaseOnline
	switch {
	case agent.Spec.Drain && active > 0:
		phase = buildv1alpha1.MacAgentPhaseDraining
	case agent.Spec.Drain:
		phase = buildv1alpha1.MacAgentPhaseDrained
	case agent.Spec.Unschedulable:
		phase = buildv1alpha1.MacAgentPhaseCordoned
	}
	return phase, true, heartbeatAge
}

func (r *MacAgentReconciler) setReadyCondition(
	status *buildv1alpha1.MacAgentStatus,
	agent *buildv1alpha1.MacAgent,
	ready bool,
	heartbeatAge string,
	now time.Time,
) {
	condition := metav1.Condition{
		Type:               buildv1alpha1.MacAgentConditionReady,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: metav1.NewTime(now),
	}
	switch {
	case status.LastHeartbeatTime == nil:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonNoHeartbeat
		condition.Message = "no heartbeat received yet"
	case !ready:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonHeartbeatLost
		condition.Message = fmt.Sprintf("last heartbeat %s ago (threshold %s)", heartbeatAge, humanDuration(r.offlineThreshold()))
	default:
		condition.Status = metav1.ConditionTrue
		condition.Reason = buildv1alpha1.MacAgentReasonHeartbeatOK
		condition.Message = fmt.Sprintf("heartbeat received %s ago", heartbeatAge)
	}
	meta.SetStatusCondition(&status.Conditions, condition)
}

func (r *MacAgentReconciler) setSchedulableCondition(
	status *buildv1alpha1.MacAgentStatus,
	agent *buildv1alpha1.MacAgent,
	ready bool,
	active int32,
	capacity int32,
) {
	condition := metav1.Condition{
		Type:               buildv1alpha1.MacAgentConditionSchedulable,
		ObservedGeneration: agent.Generation,
	}
	switch {
	case !ready:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonNotReady
		condition.Message = "agent is not ready"
	case agent.Spec.Drain:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonDraining
		condition.Message = "agent is draining"
	case agent.Spec.Unschedulable:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonCordoned
		condition.Message = "agent is cordoned"
	case active >= capacity:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonCapacityFull
		condition.Message = fmt.Sprintf("%d/%d build slots free", capacity-active, capacity)
	case diskPressure(status) || status.Runtime.DiskFreeBytes < agent.Spec.MinFreeDiskBytesOrDefault():
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonDiskPressure
		condition.Message = fmt.Sprintf("disk free %s is below the threshold", humanDisk(status.Runtime.DiskFreeBytes))
	default:
		condition.Status = metav1.ConditionTrue
		condition.Reason = buildv1alpha1.MacAgentReasonSchedulable
		condition.Message = fmt.Sprintf("%d/%d build slots free", capacity-active, capacity)
	}
	meta.SetStatusCondition(&status.Conditions, condition)
}

func (r *MacAgentReconciler) setDrainingCondition(status *buildv1alpha1.MacAgentStatus, agent *buildv1alpha1.MacAgent, active int32) {
	condition := metav1.Condition{
		Type:               buildv1alpha1.MacAgentConditionDraining,
		ObservedGeneration: agent.Generation,
	}
	if agent.Spec.Drain {
		condition.Status = metav1.ConditionTrue
		condition.Reason = buildv1alpha1.MacAgentReasonDraining
		condition.Message = fmt.Sprintf("%d active builds remaining", active)
	} else {
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonNotDraining
		condition.Message = "not draining"
	}
	meta.SetStatusCondition(&status.Conditions, condition)
}

func (r *MacAgentReconciler) assignedBuilds(ctx context.Context, agentName string) ([]buildv1alpha1.MacBuild, error) {
	var list buildv1alpha1.MacBuildList
	opts := []client.ListOption{}
	if r.MacBuildNamespace != "" {
		opts = append(opts, client.InNamespace(r.MacBuildNamespace))
	}
	if err := r.List(ctx, &list, opts...); err != nil {
		return nil, err
	}

	assigned := make([]buildv1alpha1.MacBuild, 0, len(list.Items))
	for _, build := range list.Items {
		if build.Status.Assignment == nil || build.Status.Assignment.AgentName != agentName {
			continue
		}
		assigned = append(assigned, build)
	}
	return assigned, nil
}

func (r *MacAgentReconciler) reconcileBuilds(
	ctx context.Context,
	agent *buildv1alpha1.MacAgent,
	builds []buildv1alpha1.MacBuild,
	now time.Time,
	ready bool,
) error {
	for i := range builds {
		build := &builds[i]
		if !isStartedBuildPhase(build.Status.Phase) {
			continue
		}
		if build.Status.Assignment == nil || build.Status.Assignment.AgentName != agent.Name {
			continue
		}

		if ready {
			if isAgentGraceMessage(build.Status.PhaseMessage, agent.Name) {
				message := fmt.Sprintf("agent %s reconnected; build continues.", agent.Name)
				if err := r.setBuildPhaseMessage(ctx, build, agent.Name, message); err != nil {
					return err
				}
				if r.Recorder != nil {
					r.Recorder.Event(build, "Normal", "AgentReconnected", message)
				}
			}
			continue
		}

		if agent.Status.LastHeartbeatTime == nil {
			continue
		}
		lastSeen := agent.Status.LastHeartbeatTime.Time
		grace := agent.Spec.OfflineGracePeriodOrDefault()
		deadline := lastSeen.Add(grace)
		if now.Before(deadline) {
			message := fmt.Sprintf(
				"agent %s unreachable since %s; build will fail at %s unless the agent reconnects",
				agent.Name, lastSeen.UTC().Format(time.RFC3339), deadline.UTC().Format(time.RFC3339),
			)
			if err := r.setBuildPhaseMessage(ctx, build, agent.Name, message); err != nil {
				return err
			}
			continue
		}

		message := fmt.Sprintf(
			"agent %s stopped reporting at %s (last phase %s); build failed after %s grace. Retry by creating a new build.",
			agent.Name, lastSeen.UTC().Format(time.RFC3339), build.Status.Phase, humanDuration(grace),
		)
		if err := r.failBuildForAgentLost(ctx, build, agent.Name, message, now); err != nil {
			return err
		}
		if r.Recorder != nil {
			r.Recorder.Event(build, "Warning", "AgentLost", message)
		}
	}
	return nil
}

func (r *MacAgentReconciler) setBuildPhaseMessage(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	agentName string,
	message string,
) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if !isStartedBuildPhase(latest.Status.Phase) {
			return nil
		}
		if latest.Status.Assignment == nil || latest.Status.Assignment.AgentName != agentName {
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

func (r *MacAgentReconciler) failBuildForAgentLost(
	ctx context.Context,
	build *buildv1alpha1.MacBuild,
	agentName string,
	message string,
	now time.Time,
) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest buildv1alpha1.MacBuild
		if err := r.Get(ctx, client.ObjectKeyFromObject(build), &latest); err != nil {
			return err
		}
		if !isStartedBuildPhase(latest.Status.Phase) {
			return nil
		}
		if latest.Status.Assignment == nil || latest.Status.Assignment.AgentName != agentName {
			return nil
		}
		base := latest.DeepCopy()
		at := metav1.NewTime(now)
		latest.Status.SetPhase(buildv1alpha1.PhaseFailed, message, at)
		latest.Status.Message = &message
		latest.Status.CompletionTime = &at
		return r.Status().Patch(ctx, &latest, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
}

func isAgentGraceMessage(message *string, agentName string) bool {
	if message == nil {
		return false
	}
	return strings.HasPrefix(*message, fmt.Sprintf("agent %s unreachable since", agentName))
}

func isStartedBuildPhase(phase string) bool {
	switch phase {
	case buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding, buildv1alpha1.PhasePublishing:
		return true
	default:
		return false
	}
}

func (r *MacAgentReconciler) recordEvents(agent *buildv1alpha1.MacAgent, oldPhase string, oldReady *metav1.Condition) {
	if r.Recorder == nil {
		return
	}
	ready := meta.FindStatusCondition(agent.Status.Conditions, buildv1alpha1.MacAgentConditionReady)
	if ready != nil && oldReady != nil && oldReady.Status != ready.Status {
		switch ready.Status {
		case metav1.ConditionFalse:
			r.Recorder.Event(agent, "Warning", buildv1alpha1.MacAgentReasonHeartbeatLost, ready.Message)
		case metav1.ConditionTrue:
			r.Recorder.Event(agent, "Normal", "AgentReconnected", ready.Message)
		}
	}
	if agent.Status.Phase != oldPhase {
		switch agent.Status.Phase {
		case buildv1alpha1.MacAgentPhaseCordoned:
			r.Recorder.Event(agent, "Normal", buildv1alpha1.MacAgentPhaseCordoned, "agent cordoned")
		case buildv1alpha1.MacAgentPhaseDrained:
			r.Recorder.Event(agent, "Normal", buildv1alpha1.MacAgentPhaseDrained, "agent drained")
		}
	}
}

func (r *MacAgentReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *MacAgentReconciler) offlineThreshold() time.Duration {
	if r.OfflineThreshold > 0 {
		return r.OfflineThreshold
	}
	return defaultAgentOfflineThreshold
}

func (r *MacAgentReconciler) requeueInterval() time.Duration {
	if r.RequeueInterval > 0 {
		return r.RequeueInterval
	}
	return defaultAgentRequeueInterval
}

func onlineDuration(connectedSince, lastHeartbeat *metav1.Time, ready bool, now time.Time) string {
	if connectedSince == nil {
		return ""
	}
	end := now
	if !ready {
		if lastHeartbeat == nil {
			return ""
		}
		end = lastHeartbeat.Time
	}
	if end.Before(connectedSince.Time) {
		return "0s"
	}
	return humanDuration(end.Sub(connectedSince.Time))
}

func diskPressure(status *buildv1alpha1.MacAgentStatus) bool {
	condition := meta.FindStatusCondition(status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	return condition != nil && condition.Status == metav1.ConditionTrue
}

func statusEqual(a, b buildv1alpha1.MacAgentStatus) bool {
	if a.Phase != b.Phase ||
		a.Summary != b.Summary ||
		a.OnlineDuration != b.OnlineDuration ||
		a.ActiveBuilds != b.ActiveBuilds ||
		a.Stats != b.Stats ||
		len(a.AssignedBuilds) != len(b.AssignedBuilds) {
		return false
	}
	for i := range a.AssignedBuilds {
		if a.AssignedBuilds[i] != b.AssignedBuilds[i] {
			return false
		}
	}
	return managedConditionsEqual(a.Conditions, b.Conditions)
}

func managedConditionsEqual(a, b []metav1.Condition) bool {
	for _, conditionType := range []string{
		buildv1alpha1.MacAgentConditionReady,
		buildv1alpha1.MacAgentConditionSchedulable,
		buildv1alpha1.MacAgentConditionDraining,
	} {
		left := meta.FindStatusCondition(a, conditionType)
		right := meta.FindStatusCondition(b, conditionType)
		if (left == nil) != (right == nil) {
			return false
		}
		if left != nil && (left.Status != right.Status || left.Reason != right.Reason || left.Message != right.Message) {
			return false
		}
	}
	return true
}
