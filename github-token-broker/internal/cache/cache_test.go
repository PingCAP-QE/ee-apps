package cache

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestGetOrMintCaches(t *testing.T) {
	c := New(time.Minute, 10)
	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		return Token{Value: "token", ExpiresAt: time.Now().Add(time.Hour), Source: "app"}, nil
	}
	first, err := c.GetOrMint(context.Background(), "key", mint)
	if err != nil {
		t.Fatal(err)
	}
	second, err := c.GetOrMint(context.Background(), "key", mint)
	if err != nil {
		t.Fatal(err)
	}
	if first.Value != second.Value || calls != 1 {
		t.Fatalf("expected a cached token, calls=%d", calls)
	}
}

func TestGetOrMintDoesNotCacheWithinMargin(t *testing.T) {
	c := New(time.Minute, 10)
	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		return Token{Value: "token", ExpiresAt: time.Now().Add(30 * time.Second)}, nil
	}
	for i := 0; i < 2; i++ {
		if _, err := c.GetOrMint(context.Background(), "key", mint); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 2 {
		t.Fatalf("expected two mints, got %d", calls)
	}
}

func TestGetOrMintDoesNotCacheWithoutExpiry(t *testing.T) {
	c := New(time.Minute, 10)
	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		return Token{Value: "pat", Source: "pat"}, nil
	}
	for i := 0; i < 2; i++ {
		if _, err := c.GetOrMint(context.Background(), "key", mint); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 2 {
		t.Fatalf("expected two mints, got %d", calls)
	}
}

func TestGetOrMintDoesNotServeTokensBelowMinRemaining(t *testing.T) {
	// Callers share a cache key (owner/repo/permissions) but not their clocks:
	// here the entry was minted 50 minutes ago for another caller, so only 10 of
	// the required 15 minutes are left. Serving it would expire it mid-job.
	c := New(time.Minute, 10)
	c.SetMinRemaining(15 * time.Minute)
	base := time.Now()
	c.tokens.Set("key", Token{Value: "shared", ExpiresAt: base.Add(time.Hour), Source: "app"}, time.Hour)
	c.now = func() time.Time { return base.Add(50 * time.Minute) }

	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		return Token{Value: "fresh", ExpiresAt: c.now().Add(time.Hour), Source: "app"}, nil
	}
	token, err := c.GetOrMint(context.Background(), "key", mint)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || token.Value != "fresh" {
		t.Fatalf("expected a fresh mint, got %q after %d mints", token.Value, calls)
	}
}

func TestGetOrMintServesTokensAboveMinRemaining(t *testing.T) {
	c := New(time.Minute, 10)
	c.SetMinRemaining(15 * time.Minute)
	base := time.Now()
	c.now = func() time.Time { return base }
	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		return Token{Value: "token", ExpiresAt: base.Add(time.Hour), Source: "app"}, nil
	}
	for i := 0; i < 3; i++ {
		if _, err := c.GetOrMint(context.Background(), "key", mint); err != nil {
			t.Fatal(err)
		}
	}
	// 40 minutes in, an hour-long token is still 20 minutes from expiry.
	c.now = func() time.Time { return base.Add(40 * time.Minute) }
	if _, err := c.GetOrMint(context.Background(), "key", mint); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("expected one mint, got %d", calls)
	}
}

func TestGetOrMintSingleflight(t *testing.T) {
	c := New(time.Minute, 10)
	var calls int32
	mint := func(context.Context) (Token, error) {
		atomic.AddInt32(&calls, 1)
		time.Sleep(50 * time.Millisecond)
		return Token{Value: "token", ExpiresAt: time.Now().Add(time.Hour)}, nil
	}

	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := c.GetOrMint(context.Background(), "key", mint); err != nil {
				t.Errorf("unexpected error: %v", err)
			}
		}()
	}
	wg.Wait()
	if calls != 1 {
		t.Fatalf("expected a single mint, got %d", calls)
	}
}

func TestEviction(t *testing.T) {
	c := New(time.Minute, 1)
	mint := func(context.Context) (Token, error) {
		return Token{Value: "token", ExpiresAt: time.Now().Add(time.Hour)}, nil
	}
	if _, err := c.GetOrMint(context.Background(), "one", mint); err != nil {
		t.Fatal(err)
	}
	if _, err := c.GetOrMint(context.Background(), "two", mint); err != nil {
		t.Fatal(err)
	}
	if got := c.tokens.Len(); got > 1 {
		t.Fatalf("expected at most one entry, got %d", got)
	}
}
