package v1alpha1

import (
	"encoding/json"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestMacAgentSpecDefaults(t *testing.T) {
	t.Parallel()

	var spec MacAgentSpec
	if got := spec.CapacityOrDefault(); got != DefaultMacAgentCapacity {
		t.Fatalf("expected default capacity %d, got %d", DefaultMacAgentCapacity, got)
	}
	if got := spec.MinFreeDiskBytesOrDefault(); got != DefaultMacAgentMinFreeDiskBytes {
		t.Fatalf("expected default minFreeDiskBytes %d, got %d", DefaultMacAgentMinFreeDiskBytes, got)
	}
	if got := spec.OfflineGracePeriodOrDefault(); got != DefaultMacAgentOfflineGracePeriod {
		t.Fatalf("expected default offlineGracePeriod %s, got %s", DefaultMacAgentOfflineGracePeriod, got)
	}

	spec = MacAgentSpec{
		Capacity:           2,
		MinFreeDiskBytes:   2 * 1024 * 1024 * 1024,
		OfflineGracePeriod: &metav1.Duration{Duration: 5 * time.Minute},
	}
	if got := spec.CapacityOrDefault(); got != 2 {
		t.Fatalf("expected explicit capacity 2, got %d", got)
	}
	if got := spec.MinFreeDiskBytesOrDefault(); got != 2*1024*1024*1024 {
		t.Fatalf("expected explicit minFreeDiskBytes, got %d", got)
	}
	if got := spec.OfflineGracePeriodOrDefault(); got != 5*time.Minute {
		t.Fatalf("expected explicit offlineGracePeriod 5m, got %s", got)
	}
}

func TestMacAgentStatusJSONRoundTrip(t *testing.T) {
	t.Parallel()

	connected := metav1.NewTime(time.Date(2026, 11, 1, 8, 0, 0, 0, time.UTC))
	heartbeat := metav1.NewTime(connected.Add(3*time.Hour + 12*time.Minute))
	status := MacAgentStatus{
		Phase:             MacAgentPhaseOnline,
		Summary:           "Online 3h12m | 10.20.30.40 | arm64 | 8C/16GiB",
		Hostname:          "mac-mini-01",
		IP:                "10.20.30.40",
		Addresses:         []MacAgentAddress{{Type: MacAgentAddressTypeInternalIP, Address: "10.20.30.40"}},
		MachineID:         MacAgentMachineID{Source: MacAgentMachineIDSourcePlatformUUID, Value: "3F2504E0-4F89-11D3-9A0C-0305E82C3301"},
		Arch:              BuildArchARM64,
		OS:                MacAgentOS{Version: "14.6.1", Build: "23G93", Kernel: "Darwin 23.6.0"},
		Hardware:          MacAgentHardware{Model: "Mac14,2", CPUModel: "Apple M2", CPUCores: 8, MemoryBytes: 17179869184},
		Runtime:           MacAgentRuntime{Load1: 1.25, Load5: 1.10, Load15: 0.95, DiskFreeBytes: 249108103168, DiskFreePercent: 50},
		AgentVersion:      "v2026.11.1",
		InstanceID:        "6f1c2d9e-0000-0000-0000-000000000000",
		ConnectedSince:    &connected,
		OnlineDuration:    "3h12m",
		LastHeartbeatTime: &heartbeat,
		ActiveBuilds:      1,
		AssignedBuilds:    []MacBuildReference{{Namespace: "ee-cd", Name: "macbuild-abc"}},
		Stats:             MacAgentStats{Completed: 42, Failed: 3},
		Conditions: []metav1.Condition{{
			Type:               MacAgentConditionReady,
			Status:             metav1.ConditionTrue,
			Reason:             MacAgentReasonHeartbeatOK,
			LastTransitionTime: connected,
		}},
	}

	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("marshal status: %v", err)
	}
	var back MacAgentStatus
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal status: %v", err)
	}
	if back.Phase != MacAgentPhaseOnline || back.Hostname != "mac-mini-01" || back.IP != "10.20.30.40" {
		t.Fatalf("unexpected round-trip status: %#v", back)
	}
	if back.Hardware.CPUCores != 8 || back.Runtime.Load1 != 1.25 || back.Stats.Completed != 42 {
		t.Fatalf("unexpected nested round-trip status: %#v", back)
	}
	if back.AssignedBuilds[0].Name != "macbuild-abc" {
		t.Fatalf("unexpected assigned build: %#v", back.AssignedBuilds)
	}
}

func TestMacAgentSpecJSONRoundTrip(t *testing.T) {
	t.Parallel()

	spec := MacAgentSpec{
		Unschedulable:      true,
		Drain:              true,
		Capacity:           2,
		MinFreeDiskBytes:   20 * 1024 * 1024 * 1024,
		OfflineGracePeriod: &metav1.Duration{Duration: 10 * time.Minute},
	}
	data, err := json.Marshal(spec)
	if err != nil {
		t.Fatalf("marshal spec: %v", err)
	}
	var back MacAgentSpec
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal spec: %v", err)
	}
	if !back.Unschedulable || !back.Drain || back.Capacity != 2 {
		t.Fatalf("unexpected spec round-trip: %#v", back)
	}
	if back.OfflineGracePeriod == nil || back.OfflineGracePeriod.Duration != 10*time.Minute {
		t.Fatalf("unexpected offlineGracePeriod: %#v", back.OfflineGracePeriod)
	}
}
