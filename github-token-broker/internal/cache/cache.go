package cache

import (
	"context"
	"sync/atomic"
	"time"

	"github.com/jellydator/ttlcache/v3"
	"golang.org/x/sync/singleflight"
)

// Token is a cached credential with its origin metadata.
type Token struct {
	Value          string
	ExpiresAt      time.Time
	InstallationID int64
	Source         string
}

// Cache stores tokens until shortly before they expire and coalesces
// concurrent mint requests for the same key.
type Cache struct {
	tokens       *ttlcache.Cache[string, Token]
	group        singleflight.Group
	ttlMargin    atomic.Int64
	minRemaining atomic.Int64
	now          func() time.Time
}

// New creates a cache. ttlMargin is subtracted from a token's expiry when
// deciding how long the token may be cached.
func New(ttlMargin time.Duration, maxEntries int) *Cache {
	if maxEntries < 1 {
		maxEntries = 1
	}
	tokens := ttlcache.New[string, Token](
		ttlcache.WithCapacity[string, Token](uint64(maxEntries)),
		ttlcache.WithDisableTouchOnHit[string, Token](),
	)
	go tokens.Start()
	cache := &Cache{
		tokens: tokens,
		now:    time.Now,
	}
	cache.ttlMargin.Store(int64(ttlMargin))
	return cache
}

// SetTTLMargin updates the margin subtracted from token expiry before caching.
func (c *Cache) SetTTLMargin(margin time.Duration) {
	c.ttlMargin.Store(int64(margin))
}

// SetMinRemaining is the least time a caller may expect to keep using a cached
// token. Entries are keyed by what the caller asked for, not by who asked, so
// several callers share one token: without this floor the last caller can be
// handed a token that expires while it is still working. A cached token closer
// to expiry than this is re-minted instead of served.
func (c *Cache) SetMinRemaining(remaining time.Duration) {
	c.minRemaining.Store(int64(remaining))
}

// GetOrMint returns a cached token for key, or calls mint once for concurrent
// callers. Tokens without an expiry are never cached, and a cached token is
// only served while at least minRemaining is left on it.
func (c *Cache) GetOrMint(ctx context.Context, key string, mint func(context.Context) (Token, error)) (Token, error) {
	if token, ok := c.usable(key); ok {
		return token, nil
	}

	value, err, _ := c.group.Do(key, func() (any, error) {
		if token, ok := c.usable(key); ok {
			return token, nil
		}
		token, err := mint(ctx)
		if err != nil || token.ExpiresAt.IsZero() {
			return token, err
		}
		// Do not hold an entry that may never be served.
		margin := time.Duration(c.ttlMargin.Load())
		if remaining := time.Duration(c.minRemaining.Load()); remaining > margin {
			margin = remaining
		}
		if ttl := token.ExpiresAt.Sub(c.now()) - margin; ttl > 0 {
			c.tokens.Set(key, token, ttl)
		}
		return token, nil
	})
	if err != nil {
		return Token{}, err
	}
	return value.(Token), nil
}

// usable reports a cached token the caller can rely on for minRemaining.
func (c *Cache) usable(key string) (Token, bool) {
	item := c.tokens.Get(key)
	if item == nil {
		return Token{}, false
	}
	token := item.Value()
	if remaining := token.ExpiresAt.Sub(c.now()); remaining < time.Duration(c.minRemaining.Load()) {
		return Token{}, false
	}
	return token, true
}
