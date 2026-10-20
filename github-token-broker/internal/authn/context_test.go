package authn

import (
	"context"
	"testing"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
)

func TestCallerContext(t *testing.T) {
	if _, ok := CallerFromContext(context.Background()); ok {
		t.Fatal("did not expect a caller")
	}
	caller := authz.Caller{Namespace: "ci", ServiceAccount: "checkout"}
	ctx := WithCaller(context.Background(), caller)
	got, ok := CallerFromContext(ctx)
	if !ok || got != caller {
		t.Fatalf("unexpected caller: %#v %v", got, ok)
	}
}
