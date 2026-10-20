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
