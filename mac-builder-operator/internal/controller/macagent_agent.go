package controller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"math"
	mathrand "math/rand"
	"reflect"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/identity"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/inventory"
)

// AgentInventory collects the inventory reported by a macOS worker.
type AgentInventory interface {
	CollectStatic(context.Context) inventory.Static
	CollectRuntime(context.Context) buildv1alpha1.MacAgentRuntime
}

// MacAgentHeartbeat registers the worker's MacAgent and keeps its status fresh.
// It runs as a manager Runnable on the macOS worker, not as a reconciler: the
// heartbeat writes status on a timer and must not react to its own writes.
type MacAgentHeartbeat struct {
	Client    client.Client
	APIReader client.Reader

	AgentName    string
	AgentArch    string
	AgentVersion string
	Capacity     int32
	Labels       map[string]string

	Machine   identity.Machine
	Inventory AgentInventory

	HeartbeatInterval     time.Duration
	StaticRefreshInterval time.Duration
	Now                   func() time.Time
	NewInstanceID         func() string

	instanceID     string
	connectedSince metav1.Time
	static         inventory.Static
	staticAt       time.Time
}

// Start runs the heartbeat loop until the manager context is cancelled.
func (h *MacAgentHeartbeat) Start(ctx context.Context) error {
	logger := logf.FromContext(ctx).WithName("macagent-heartbeat")
	h.init(h.now())

	timer := time.NewTimer(h.nextHeartbeatInterval())
	defer timer.Stop()

	for {
		syncCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := h.syncOnce(syncCtx)
		cancel()
		if err != nil && ctx.Err() == nil {
			logger.Error(err, "register/heartbeat sync failed")
		}

		select {
		case <-ctx.Done():
			return nil
		case <-timer.C:
			timer.Reset(h.nextHeartbeatInterval())
		}
	}
}

// NeedLeaderElection is false: every worker runs its own heartbeat.
func (h *MacAgentHeartbeat) NeedLeaderElection() bool {
	return false
}

func (h *MacAgentHeartbeat) syncOnce(ctx context.Context) error {
	if strings.TrimSpace(h.AgentName) == "" {
		return fmt.Errorf("agent name must not be empty")
	}
	if h.Client == nil {
		return fmt.Errorf("client is not configured")
	}
	if h.Inventory == nil {
		return fmt.Errorf("inventory is not configured")
	}

	now := h.now()
	h.init(now)
	if h.staticAt.IsZero() || now.Sub(h.staticAt) >= h.staticRefreshInterval() {
		h.static = h.Inventory.CollectStatic(ctx)
		h.staticAt = now
	}
	runtime := h.Inventory.CollectRuntime(ctx)

	reader := h.APIReader
	if reader == nil {
		reader = h.Client
	}

	key := types.NamespacedName{Name: h.AgentName}
	var agent buildv1alpha1.MacAgent
	err := reader.Get(ctx, key, &agent)
	if apierrors.IsNotFound(err) {
		agent = buildv1alpha1.MacAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:   h.AgentName,
				Labels: h.desiredLabels(),
			},
		}
		if h.Capacity > 0 {
			agent.Spec.Capacity = h.Capacity
		}
		if createErr := h.Client.Create(ctx, &agent); createErr != nil {
			if !apierrors.IsAlreadyExists(createErr) {
				return fmt.Errorf("create MacAgent: %w", createErr)
			}
			if getErr := reader.Get(ctx, key, &agent); getErr != nil {
				return fmt.Errorf("get MacAgent after create conflict: %w", getErr)
			}
		}
	} else if err != nil {
		return fmt.Errorf("get MacAgent: %w", err)
	}

	if err := h.ensureLabels(ctx, &agent); err != nil {
		return err
	}
	return h.patchStatus(ctx, &agent, runtime, now)
}

func (h *MacAgentHeartbeat) ensureLabels(ctx context.Context, agent *buildv1alpha1.MacAgent) error {
	desired := h.desiredLabels()
	if labelsMatch(agent.Labels, desired) {
		return nil
	}
	base := agent.DeepCopy()
	if agent.Labels == nil {
		agent.Labels = map[string]string{}
	}
	for key, value := range desired {
		agent.Labels[key] = value
	}
	if err := h.Client.Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("patch MacAgent labels: %w", err)
	}
	return nil
}

func (h *MacAgentHeartbeat) patchStatus(
	ctx context.Context,
	agent *buildv1alpha1.MacAgent,
	runtime buildv1alpha1.MacAgentRuntime,
	now time.Time,
) error {
	base := agent.DeepCopy()
	h.applyStatus(&agent.Status, &agent.Spec, runtime, now)
	if reflect.DeepEqual(base.Status, agent.Status) {
		return nil
	}
	if err := h.Client.Status().Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("patch MacAgent status: %w", err)
	}
	return nil
}

func (h *MacAgentHeartbeat) applyStatus(
	status *buildv1alpha1.MacAgentStatus,
	spec *buildv1alpha1.MacAgentSpec,
	runtime buildv1alpha1.MacAgentRuntime,
	now time.Time,
) {
	status.Hostname = h.static.Hostname
	status.IP = h.static.IP
	status.Addresses = append([]buildv1alpha1.MacAgentAddress(nil), h.static.Addresses...)
	status.MachineID = buildv1alpha1.MacAgentMachineID{
		Source: h.Machine.Source,
		Value:  h.Machine.Value,
	}
	status.Arch = buildv1alpha1.NormalizeBuildArch(h.AgentArch)
	status.OS = h.static.OS
	status.Hardware = h.static.Hardware
	status.Runtime = runtime
	status.AgentVersion = h.AgentVersion
	status.InstanceID = h.instanceID
	connected := h.connectedSince
	status.ConnectedSince = &connected
	heartbeat := metav1.NewTime(now)
	status.LastHeartbeatTime = &heartbeat
	h.setDiskPressureCondition(status, spec, runtime, now)
}

func (h *MacAgentHeartbeat) setDiskPressureCondition(
	status *buildv1alpha1.MacAgentStatus,
	spec *buildv1alpha1.MacAgentSpec,
	runtime buildv1alpha1.MacAgentRuntime,
	now time.Time,
) {
	threshold := spec.MinFreeDiskBytesOrDefault()
	condition := metav1.Condition{
		Type:               buildv1alpha1.MacAgentConditionDiskPressure,
		LastTransitionTime: metav1.NewTime(now),
	}
	switch {
	case runtime.DiskFreeBytes < 0:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonDiskUnknown
		condition.Message = "disk usage is not available"
	case runtime.DiskFreeBytes == 0:
		condition.Status = metav1.ConditionTrue
		condition.Reason = buildv1alpha1.MacAgentReasonDiskPressure
		condition.Message = "0 B free (disk full)"
	case runtime.DiskFreeBytes < threshold:
		condition.Status = metav1.ConditionTrue
		condition.Reason = buildv1alpha1.MacAgentReasonDiskPressure
		condition.Message = fmt.Sprintf("%s free (< %s)", humanDisk(runtime.DiskFreeBytes), humanDisk(threshold))
	default:
		condition.Status = metav1.ConditionFalse
		condition.Reason = buildv1alpha1.MacAgentReasonSufficientDisk
		condition.Message = fmt.Sprintf("%s free (>= %s)", humanDisk(runtime.DiskFreeBytes), humanDisk(threshold))
	}
	// Keep the transition time stable while the status and reason are unchanged
	// so an unchanged heartbeat does not rewrite the condition on every sync.
	if existing := meta.FindStatusCondition(status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure); existing != nil &&
		existing.Status == condition.Status && existing.Reason == condition.Reason {
		condition.LastTransitionTime = existing.LastTransitionTime
	}
	meta.SetStatusCondition(&status.Conditions, condition)
}

func (h *MacAgentHeartbeat) desiredLabels() map[string]string {
	labels := make(map[string]string, len(h.Labels)+1)
	for key, value := range h.Labels {
		labels[key] = value
	}
	if arch := buildv1alpha1.NormalizeBuildArch(h.AgentArch); arch != "" {
		labels["tibuild.pingcap.net/arch"] = arch
	}
	return labels
}

func (h *MacAgentHeartbeat) init(now time.Time) {
	if h.instanceID == "" {
		h.instanceID = h.newInstanceID()
	}
	if h.connectedSince.IsZero() {
		h.connectedSince = metav1.NewTime(now)
	}
}

func (h *MacAgentHeartbeat) newInstanceID() string {
	if h.NewInstanceID != nil {
		return h.NewInstanceID()
	}
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return fmt.Sprintf("instance-%d", h.now().UnixNano())
	}
	return hex.EncodeToString(buf[:])
}

func (h *MacAgentHeartbeat) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

func (h *MacAgentHeartbeat) heartbeatInterval() time.Duration {
	if h.HeartbeatInterval > 0 {
		return h.HeartbeatInterval
	}
	return 20 * time.Second
}

// nextHeartbeatInterval applies ±20% jitter so a fleet of workers does not
// heartbeat in lockstep.
func (h *MacAgentHeartbeat) nextHeartbeatInterval() time.Duration {
	base := h.heartbeatInterval()
	delta := int64(math.Max(1, float64(base)/5))
	offset := time.Duration(mathrand.Int63n(2*delta+1) - delta)
	return base + offset
}

func (h *MacAgentHeartbeat) staticRefreshInterval() time.Duration {
	if h.StaticRefreshInterval > 0 {
		return h.StaticRefreshInterval
	}
	return 10 * time.Minute
}

func labelsMatch(existing, desired map[string]string) bool {
	for key, value := range desired {
		if existing[key] != value {
			return false
		}
	}
	return true
}
