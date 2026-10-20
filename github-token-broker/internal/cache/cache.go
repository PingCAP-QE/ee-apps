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
	tokens    *ttlcache.Cache[string, Token]
	group     singleflight.Group
	ttlMargin atomic.Int64
	now       func() time.Time
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

// GetOrMint returns a cached token for key, or calls mint once for concurrent
// callers. Tokens without an expiry are never cached.
func (c *Cache) GetOrMint(ctx context.Context, key string, mint func(context.Context) (Token, error)) (Token, error) {
	if item := c.tokens.Get(key); item != nil {
		return item.Value(), nil
	}

	value, err, _ := c.group.Do(key, func() (any, error) {
		if item := c.tokens.Get(key); item != nil {
			return item.Value(), nil
		}
		token, err := mint(ctx)
		if err != nil || token.ExpiresAt.IsZero() {
			return token, err
		}
		if ttl := token.ExpiresAt.Sub(c.now()) - time.Duration(c.ttlMargin.Load()); ttl > 0 {
			c.tokens.Set(key, token, ttl)
		}
		return token, nil
	})
	if err != nil {
		return Token{}, err
	}
	return value.(Token), nil
}
