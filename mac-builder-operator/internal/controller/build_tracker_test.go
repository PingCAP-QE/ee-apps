package controller

import (
	"testing"
	"time"
)

func TestBuildTrackerWaitTimeout(t *testing.T) {
	t.Parallel()

	tracker := NewBuildTracker()
	if !tracker.WaitTimeout(10 * time.Millisecond) {
		t.Fatal("expected an empty tracker to be ready immediately")
	}

	tracker.Begin()
	if tracker.WaitTimeout(10 * time.Millisecond) {
		t.Fatal("expected a timeout while a build is active")
	}

	tracker.End()
	if !tracker.WaitTimeout(time.Second) {
		t.Fatal("expected the tracker to be ready after the build ended")
	}
}
