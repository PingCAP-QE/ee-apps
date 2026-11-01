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
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/identity"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/inventory"
)

type fakeAgentInventory struct {
	static  inventory.Static
	runtime buildv1alpha1.MacAgentRuntime
}

func (f fakeAgentInventory) CollectStatic(context.Context) inventory.Static {
	return f.static
}

func (f fakeAgentInventory) CollectRuntime(context.Context) buildv1alpha1.MacAgentRuntime {
	return f.runtime
}

func newAgentHeartbeat(client *fake.ClientBuilder, now time.Time) *MacAgentHeartbeat {
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		panic(err)
	}
	c := client.WithScheme(scheme).WithStatusSubresource(&buildv1alpha1.MacAgent{}).Build()
	return &MacAgentHeartbeat{
		Client:                c,
		APIReader:             c,
		AgentName:             "mac-test",
		AgentArch:             buildv1alpha1.BuildArchARM64,
		AgentVersion:          "v-test",
		Capacity:              1,
		Labels:                map[string]string{"tibuild.pingcap.net/pool": "mac-mini"},
		Machine:               identity.Machine{Source: buildv1alpha1.MacAgentMachineIDSourcePlatformUUID, Value: "UUID-1"},
		Inventory:             fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{Load1: 1.25, DiskFreeBytes: 1000, DiskFreePercent: 50}},
		HeartbeatInterval:     20 * time.Second,
		StaticRefreshInterval: 10 * time.Minute,
		Now:                   func() time.Time { return now },
		NewInstanceID:         func() string { return "instance-1" },
	}
}

func testStatic() inventory.Static {
	return inventory.Static{
		Hostname: "mac-mini-01",
		IP:       "10.0.0.5",
		Addresses: []buildv1alpha1.MacAgentAddress{
			{Type: buildv1alpha1.MacAgentAddressTypeInternalIP, Address: "10.0.0.5"},
			{Type: buildv1alpha1.MacAgentAddressTypeHostname, Address: "mac-mini-01"},
		},
		OS:       buildv1alpha1.MacAgentOS{Version: "14.6.1", Build: "23G93", Kernel: "Darwin 23.6.0"},
		Hardware: buildv1alpha1.MacAgentHardware{Model: "Mac14,2", CPUModel: "Apple M2", CPUCores: 8, MemoryBytes: 17179869184, DiskTotalBytes: 500000},
	}
}

func TestMacAgentHeartbeatRegistersAgent(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	h := newAgentHeartbeat(fake.NewClientBuilder(), now)
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("sync: %v", err)
	}

	var got buildv1alpha1.MacAgent
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if got.Labels["tibuild.pingcap.net/arch"] != buildv1alpha1.BuildArchARM64 {
		t.Fatalf("missing arch label: %#v", got.Labels)
	}
	if got.Labels["tibuild.pingcap.net/pool"] != "mac-mini" {
		t.Fatalf("missing pool label: %#v", got.Labels)
	}
	if got.Spec.Capacity != 1 {
		t.Fatalf("unexpected capacity: %d", got.Spec.Capacity)
	}
	if got.Status.Hostname != "mac-mini-01" || got.Status.IP != "10.0.0.5" {
		t.Fatalf("unexpected identity status: %#v", got.Status)
	}
	if got.Status.OS.Version != "14.6.1" || got.Status.Hardware.CPUCores != 8 {
		t.Fatalf("unexpected inventory status: %#v", got.Status)
	}
	if got.Status.AgentVersion != "v-test" || got.Status.InstanceID != "instance-1" {
		t.Fatalf("unexpected agent metadata: %#v", got.Status)
	}
	if got.Status.ConnectedSince == nil || !got.Status.ConnectedSince.Time.Equal(now) {
		t.Fatalf("unexpected connectedSince: %#v", got.Status.ConnectedSince)
	}
	if got.Status.LastHeartbeatTime == nil || !got.Status.LastHeartbeatTime.Time.Equal(now) {
		t.Fatalf("unexpected lastHeartbeatTime: %#v", got.Status.LastHeartbeatTime)
	}
}

func TestMacAgentHeartbeatPreservesOperatorFields(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	existing := &buildv1alpha1.MacAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:   "mac-test",
			Labels: map[string]string{"tibuild.pingcap.net/pool": "mac-mini", "keep": "me"},
		},
		Spec: buildv1alpha1.MacAgentSpec{Capacity: 4, Unschedulable: true},
		Status: buildv1alpha1.MacAgentStatus{
			Phase:   buildv1alpha1.MacAgentPhaseCordoned,
			Summary: "operator summary",
		},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(&buildv1alpha1.MacAgent{}).WithObjects(existing).Build()
	h := newAgentHeartbeat(fake.NewClientBuilder(), now)
	h.Client = c
	h.APIReader = c
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("sync: %v", err)
	}

	var got buildv1alpha1.MacAgent
	if err := c.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if got.Spec.Capacity != 4 || !got.Spec.Unschedulable {
		t.Fatalf("operator spec was overwritten: %#v", got.Spec)
	}
	if got.Labels["keep"] != "me" {
		t.Fatalf("operator label was removed: %#v", got.Labels)
	}
	if got.Status.Phase != buildv1alpha1.MacAgentPhaseCordoned || got.Status.Summary != "operator summary" {
		t.Fatalf("controller status was overwritten: %#v", got.Status)
	}
	if got.Status.Hostname != "mac-mini-01" {
		t.Fatalf("agent status was not written: %#v", got.Status)
	}
}

func TestMacAgentHeartbeatUpdatesRuntimeAndKeepsConnectedSince(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	h := newAgentHeartbeat(fake.NewClientBuilder(), now)
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("first sync: %v", err)
	}

	next := now.Add(20 * time.Second)
	h.Now = func() time.Time { return next }
	h.Inventory = fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{Load1: 2.5, DiskFreeBytes: 900, DiskFreePercent: 45}}
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("second sync: %v", err)
	}

	var got buildv1alpha1.MacAgent
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if got.Status.Runtime.Load1 != 2.5 {
		t.Fatalf("runtime was not updated: %#v", got.Status.Runtime)
	}
	if got.Status.ConnectedSince == nil || !got.Status.ConnectedSince.Time.Equal(now) {
		t.Fatalf("connectedSince changed: %#v", got.Status.ConnectedSince)
	}
	if got.Status.LastHeartbeatTime == nil || !got.Status.LastHeartbeatTime.Time.Equal(next) {
		t.Fatalf("lastHeartbeatTime was not updated: %#v", got.Status.LastHeartbeatTime)
	}
}

func TestNextHeartbeatIntervalJitter(t *testing.T) {
	t.Parallel()

	h := &MacAgentHeartbeat{HeartbeatInterval: 100 * time.Second}
	for i := 0; i < 1000; i++ {
		got := h.nextHeartbeatInterval()
		if got < 80*time.Second || got > 120*time.Second {
			t.Fatalf("heartbeat interval %s outside ±20%% of 100s", got)
		}
	}
}

func TestMacAgentHeartbeatSetsDiskPressure(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	h := newAgentHeartbeat(fake.NewClientBuilder(), now)
	h.Inventory = fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{DiskFreeBytes: 1}}
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("sync: %v", err)
	}

	var got buildv1alpha1.MacAgent
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != buildv1alpha1.MacAgentReasonDiskPressure {
		t.Fatalf("unexpected disk pressure condition: %#v", condition)
	}

	h.Now = func() time.Time { return now.Add(20 * time.Second) }
	h.Inventory = fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{DiskFreeBytes: 100 * 1024 * 1024 * 1024}}
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	condition = meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != buildv1alpha1.MacAgentReasonSufficientDisk {
		t.Fatalf("unexpected recovered disk condition: %#v", condition)
	}
	recoveredAt := condition.LastTransitionTime

	h.Now = func() time.Time { return now.Add(40 * time.Second) }
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("third sync: %v", err)
	}
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	condition = meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if condition == nil || !condition.LastTransitionTime.Time.Equal(recoveredAt.Time) {
		t.Fatalf("an unchanged condition must keep its transition time: %#v", condition)
	}

	h.Now = func() time.Time { return now.Add(60 * time.Second) }
	h.Inventory = fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{DiskFreeBytes: 0}}
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("fourth sync: %v", err)
	}
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	condition = meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if condition == nil || condition.Status != metav1.ConditionTrue || !strings.Contains(condition.Message, "disk full") {
		t.Fatalf("a full disk must report pressure: %#v", condition)
	}

	h.Now = func() time.Time { return now.Add(80 * time.Second) }
	h.Inventory = fakeAgentInventory{static: testStatic(), runtime: buildv1alpha1.MacAgentRuntime{DiskFreeBytes: -1}}
	if err := h.syncOnce(context.Background()); err != nil {
		t.Fatalf("fifth sync: %v", err)
	}
	if err := h.Client.Get(context.Background(), types.NamespacedName{Name: "mac-test"}, &got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	condition = meta.FindStatusCondition(got.Status.Conditions, buildv1alpha1.MacAgentConditionDiskPressure)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != buildv1alpha1.MacAgentReasonDiskUnknown {
		t.Fatalf("unknown disk metrics must report DiskUnknown: %#v", condition)
	}
}
