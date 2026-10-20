package authn

import (
	"context"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
)

type callerKey struct{}

// WithCaller stores an authenticated caller in the context.
func WithCaller(ctx context.Context, caller authz.Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, caller)
}

// CallerFromContext returns the authenticated caller, if any.
func CallerFromContext(ctx context.Context) (authz.Caller, bool) {
	caller, ok := ctx.Value(callerKey{}).(authz.Caller)
	return caller, ok
}
