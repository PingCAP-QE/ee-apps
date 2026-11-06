package worker

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
)

func TestRunnerConsumesUntilContextCancelled(t *testing.T) {
	var calls atomic.Int32
	consumed := make(chan struct{}, 1)

	r := &Runner{
		PollInterval: 5 * time.Millisecond,
		Consume: func(ctx context.Context) {
			calls.Add(1)
			select {
			case consumed <- struct{}{}:
			default:
			}
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Run(ctx)
	}()

	select {
	case <-consumed:
	case <-time.After(2 * time.Second):
		t.Fatal("consumer was not called before timeout")
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runner did not return after context cancellation")
	}

	// No consumption may happen after Run returned.
	after := calls.Load()
	time.Sleep(20 * time.Millisecond)
	require.Equal(t, after, calls.Load(), "consumer called after runner stopped")
}

func TestRunnerDefaultsPollingInterval(t *testing.T) {
	var calls atomic.Int32
	r := &Runner{
		Logger: newTestLogger(),
		Consume: func(context.Context) {
			calls.Add(1)
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Run(ctx)
	}()

	time.Sleep(30 * time.Millisecond)
	require.Zero(t, calls.Load(), "consumer must wait for one default interval before the first poll")
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runner did not return after context cancellation")
	}
}

func TestRunnerWithoutConsumerReturnsOnCancel(t *testing.T) {
	r := &Runner{Logger: newTestLogger(), PollInterval: time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Run(ctx)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runner did not return for a cancelled context")
	}
}

func newTestLogger() *zerolog.Logger {
	logger := zerolog.Nop()
	return &logger
}
