package ratelimit

import (
	"sync"
	"time"
)

// Limiter is a per-key fixed-window rate limiter. A zero or negative limit
// disables limiting.
type Limiter struct {
	mu        sync.Mutex
	windows   map[string]window
	perMinute int
	now       func() time.Time
}

type window struct {
	start time.Time
	count int
}

// New creates a limiter that allows perMinute requests per key.
func New(perMinute int) *Limiter {
	return &Limiter{
		windows:   make(map[string]window),
		perMinute: perMinute,
		now:       time.Now,
	}
}

// SetPerMinute updates the per-minute limit.
func (l *Limiter) SetPerMinute(perMinute int) {
	if l == nil {
		return
	}
	l.mu.Lock()
	l.perMinute = perMinute
	l.mu.Unlock()
}

// Allow reports whether the key may make another request in the current window.
func (l *Limiter) Allow(key string) bool {
	if l == nil || l.perMinute <= 0 {
		return true
	}
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()

	current, ok := l.windows[key]
	if !ok || now.Sub(current.start) >= time.Minute {
		current = window{start: now.Truncate(time.Minute)}
	}
	if current.count >= l.perMinute {
		l.windows[key] = current
		return false
	}
	current.count++
	l.windows[key] = current
	if len(l.windows) > 4096 {
		for k, w := range l.windows {
			if now.Sub(w.start) >= 2*time.Minute {
				delete(l.windows, k)
			}
		}
	}
	return true
}
