package controller

import (
	"sync"
	"time"
)

// BuildTracker counts builds that are currently executing. It lets the agent
// delay manager shutdown for a bounded grace period after SIGTERM.
type BuildTracker struct {
	wg sync.WaitGroup
}

// NewBuildTracker returns an empty tracker.
func NewBuildTracker() *BuildTracker {
	return &BuildTracker{}
}

// Begin marks a build as active.
func (t *BuildTracker) Begin() {
	if t == nil {
		return
	}
	t.wg.Add(1)
}

// End marks a build as finished.
func (t *BuildTracker) End() {
	if t == nil {
		return
	}
	t.wg.Done()
}

// WaitTimeout waits for active builds up to timeout. A non-positive timeout
// waits indefinitely.
func (t *BuildTracker) WaitTimeout(timeout time.Duration) bool {
	if t == nil {
		return true
	}
	done := make(chan struct{})
	go func() {
		t.wg.Wait()
		close(done)
	}()
	if timeout <= 0 {
		<-done
		return true
	}
	select {
	case <-done:
		return true
	case <-time.After(timeout):
		return false
	}
}
