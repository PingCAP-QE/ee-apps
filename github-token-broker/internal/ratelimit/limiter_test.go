package ratelimit

import (
	"testing"
	"time"
)

func TestAllow(t *testing.T) {
	limiter := New(2)
	now := time.Unix(1000, 0)
	limiter.now = func() time.Time { return now }

	for i := 0; i < 2; i++ {
		if !limiter.Allow("a") {
			t.Fatalf("expected request %d to pass", i+1)
		}
	}
	if limiter.Allow("a") {
		t.Fatal("expected the third request to be limited")
	}
	if !limiter.Allow("b") {
		t.Fatal("expected other keys to be independent")
	}

	now = now.Add(time.Minute)
	if !limiter.Allow("a") {
		t.Fatal("expected a new window to allow the key again")
	}
}

func TestDisabled(t *testing.T) {
	if !New(0).Allow("a") {
		t.Fatal("expected a disabled limiter to allow requests")
	}
	var nilLimiter *Limiter
	if !nilLimiter.Allow("a") {
		t.Fatal("expected a nil limiter to allow requests")
	}
}

func TestSetPerMinute(t *testing.T) {
	limiter := New(1)
	if !limiter.Allow("a") {
		t.Fatal("expected the first request to pass")
	}
	limiter.SetPerMinute(2)
	if !limiter.Allow("a") {
		t.Fatal("expected the updated limit to allow a second request")
	}
	if limiter.Allow("a") {
		t.Fatal("expected the updated limit to reject a third request")
	}
}
