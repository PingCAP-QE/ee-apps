package v1alpha1

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestMacBuildSpecTTLDurationJSON(t *testing.T) {
	t.Parallel()

	spec := MacBuildSpec{Ttl: &metav1.Duration{Duration: 24 * time.Hour}}
	data, err := json.Marshal(spec)
	if err != nil {
		t.Fatalf("marshal spec: %v", err)
	}
	if !strings.Contains(string(data), `"ttl":"24h0m0s"`) {
		t.Fatalf("expected duration ttl in JSON, got %s", data)
	}
	if strings.Contains(string(data), "ttlSecondsAfterFinished") {
		t.Fatalf("legacy field must not be serialized: %s", data)
	}

	var back MacBuildSpec
	if err := json.Unmarshal([]byte(`{"ttl":"24h"}`), &back); err != nil {
		t.Fatalf("unmarshal spec: %v", err)
	}
	if back.Ttl == nil || back.Ttl.Duration != 24*time.Hour {
		t.Fatalf("expected parsed ttl 24h, got %#v", back.Ttl)
	}
}

func TestNormalizeBuildArch(t *testing.T) {
	t.Parallel()

	if got := NormalizeBuildArch(" ARM64 "); got != BuildArchARM64 {
		t.Fatalf("expected normalized arch %q, got %q", BuildArchARM64, got)
	}
}

func TestMacBuildStatusSetPhaseTracksHistoryOncePerPhase(t *testing.T) {
	t.Parallel()

	first := metav1.NewTime(time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC))
	second := metav1.NewTime(first.Add(time.Minute))

	var status MacBuildStatus
	status.SetPhase(PhasePending, "Waiting for a worker.", first)
	status.SetPhase(PhasePending, "Still waiting for a worker.", second)

	if status.Phase != PhasePending {
		t.Fatalf("expected phase %q, got %q", PhasePending, status.Phase)
	}
	if status.PhaseMessage == nil || *status.PhaseMessage != "Still waiting for a worker." {
		t.Fatalf("expected current phase message to be updated, got %#v", status.PhaseMessage)
	}
	if len(status.PhaseHistory) != 1 {
		t.Fatalf("expected one phase history entry, got %#v", status.PhaseHistory)
	}
	if status.PhaseHistory[0].Message == nil || *status.PhaseHistory[0].Message != "Waiting for a worker." {
		t.Fatalf("expected first history message to be preserved, got %#v", status.PhaseHistory[0].Message)
	}

	status.SetPhase(PhaseBuilding, "Worker claimed the build.", second)

	if len(status.PhaseHistory) != 2 {
		t.Fatalf("expected second phase to append history, got %#v", status.PhaseHistory)
	}
	if status.PhaseHistory[1].Phase != PhaseBuilding {
		t.Fatalf("expected final phase %q, got %#v", PhaseBuilding, status.PhaseHistory[1])
	}
	if !status.PhaseHistory[1].TransitionTime.Time.Equal(second.Time) {
		t.Fatalf("expected transition time %s, got %#v", second, status.PhaseHistory[1].TransitionTime)
	}
}

func TestMacBuildStatusAssignedPhaseJSON(t *testing.T) {
	t.Parallel()

	assignedAt := metav1.NewTime(time.Date(2026, 11, 1, 10, 59, 0, 0, time.UTC))
	deadline := metav1.NewTime(assignedAt.Add(2 * time.Minute))
	status := MacBuildStatus{
		Assignment: &MacBuildAssignment{
			AgentName:     "mac-3f2504e0",
			AgentUID:      "agent-uid",
			AssignedAt:    assignedAt,
			LeaseDeadline: &deadline,
			Attempt:       1,
			Restarts:      2,
		},
		AssignmentAttempts: 1,
		Logs: &MacBuildLogs{
			Available: true,
			Complete:  false,
			SizeBytes: 1024,
			UpdatedAt: &assignedAt,
			Command:   "kubectl macbuild logs -n ee-cd macbuild-abc",
		},
	}
	status.SetPhase(PhaseAssigned, "Assigned to mac-3f2504e0.", assignedAt)

	if status.Phase != PhaseAssigned {
		t.Fatalf("expected phase %q, got %q", PhaseAssigned, status.Phase)
	}
	if len(status.PhaseHistory) != 1 || status.PhaseHistory[0].Phase != PhaseAssigned {
		t.Fatalf("expected assigned history entry, got %#v", status.PhaseHistory)
	}

	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("marshal status: %v", err)
	}
	var back MacBuildStatus
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal status: %v", err)
	}
	if back.Assignment == nil || back.Assignment.AgentName != "mac-3f2504e0" || back.Assignment.Attempt != 1 {
		t.Fatalf("unexpected assignment round-trip: %#v", back.Assignment)
	}
	if back.Assignment.LeaseDeadline == nil || !back.Assignment.LeaseDeadline.Time.Equal(deadline.Time) {
		t.Fatalf("unexpected lease deadline: %#v", back.Assignment.LeaseDeadline)
	}
	if back.Logs == nil || !back.Logs.Available || back.Logs.Command == "" {
		t.Fatalf("unexpected logs round-trip: %#v", back.Logs)
	}
	if back.AssignmentAttempts != 1 {
		t.Fatalf("unexpected assignmentAttempts: %d", back.AssignmentAttempts)
	}
}

func TestMacBuildSpecWorkerSelectorJSON(t *testing.T) {
	t.Parallel()

	spec := MacBuildSpec{WorkerSelector: map[string]string{
		"tibuild.pingcap.net/pool": "mac-mini",
		"tibuild.pingcap.net/arch": BuildArchARM64,
	}}
	data, err := json.Marshal(spec)
	if err != nil {
		t.Fatalf("marshal spec: %v", err)
	}
	var back MacBuildSpec
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal spec: %v", err)
	}
	if len(back.WorkerSelector) != 2 || back.WorkerSelector["tibuild.pingcap.net/pool"] != "mac-mini" {
		t.Fatalf("unexpected workerSelector round-trip: %#v", back.WorkerSelector)
	}
}
