package v1alpha1

import (
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

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
