package decision

import (
	"context"
	"errors"
	"testing"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/identity"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
)

func modeAuthorizer(t *testing.T, globalMode, ruleMode string, checker Checker) (*Authorizer, *recorder) {
	t.Helper()
	data := []byte(`{"version":"test","routes":[{"id":"rerun","host":"example.test","route":"tibuild-machine","method":"POST","path":"/builds/{id}/rerun","priority":10,"mode":"` + ruleMode + `","checks":[{"object":"capability:tibuild.devbuild.rerun","relation":"invoke"},{"object_from":"devbuild","id_from":"path.id","relation":"can_rerun"}]}]}`)
	policyRegistry, err := registry.Parse(data)
	if err != nil {
		t.Fatalf("registry.Parse() error = %v", err)
	}
	audit := &recorder{}
	authorizer := NewAuthorizer(policyRegistry,
		map[string]Credential{"machine": {Header: "x-verified-token", Verifier: verifierFunc(func(context.Context, string) (identity.Principal, error) {
			return identity.Principal{Subject: "user:issuer|alice"}, nil
		})}}, testResolver(), checker, audit)
	return authorizer.WithDecisionMode(globalMode), audit
}

func attributesForPath(path string) *authv3.AttributeContext {
	attrs := testAttributes("token")
	attrs.Request.Http.Path = path
	return attrs
}

// TestGlobalObserveAllowsUnregisteredRoutes is the precondition for attaching
// external authorization to a live route: an unregistered path must not turn
// into a 403 while the route set is still being observed.
func TestGlobalObserveAllowsUnregisteredRoutes(t *testing.T) {
	authorizer, audit := modeAuthorizer(t, "observe", "enforce", denyChecker())
	response, err := authorizer.Authorize(context.Background(), attributesForPath("/api/v2/publish/images"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetOkResponse() == nil {
		t.Fatal("observe mode denied an unregistered route")
	}
	if len(audit.events) != 1 {
		t.Fatalf("audit events = %+v", audit.events)
	}
	event := audit.events[0]
	if !event.WouldDeny || event.Reason != "route_not_registered" || event.Outcome != "allow" {
		t.Fatalf("audit = %+v, want a recorded would-deny", event)
	}
}

func TestGlobalEnforceStillDeniesUnregisteredRoutes(t *testing.T) {
	authorizer, _ := modeAuthorizer(t, "enforce", "observe", allowChecker())
	response, err := authorizer.Authorize(context.Background(), attributesForPath("/api/v2/publish/images"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetDeniedResponse() == nil {
		t.Fatal("enforce mode allowed an unregistered route")
	}
}

// TestGlobalObserveOverridesEnforceRules keeps a shadow rollout from leaking
// production denials: the operator selected observe for the whole service.
func TestGlobalObserveOverridesEnforceRules(t *testing.T) {
	authorizer, audit := modeAuthorizer(t, "observe", "enforce", denyChecker())
	response, err := authorizer.Authorize(context.Background(), testAttributes("token"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetOkResponse() == nil {
		t.Fatal("observe mode denied through an enforce rule")
	}
	if !audit.events[0].WouldDeny || audit.events[0].Reason != "relationship_denied" {
		t.Fatalf("audit = %+v", audit.events[0])
	}
}

func TestGlobalObserveNeverSurfacesDependencyErrors(t *testing.T) {
	checker := checkerFunc{check: func(context.Context, string, string, string, bool) (bool, error) {
		return false, errors.New("openfga unavailable")
	}}
	authorizer, audit := modeAuthorizer(t, "observe", "enforce", checker)
	response, err := authorizer.Authorize(context.Background(), testAttributes("token"))
	if err != nil {
		t.Fatalf("observe mode returned a gRPC error: %v", err)
	}
	if response.GetOkResponse() == nil {
		t.Fatal("observe mode denied on a dependency error")
	}
	if audit.events[0].Reason != "openfga_error" || !audit.events[0].WouldDeny {
		t.Fatalf("audit = %+v", audit.events[0])
	}
}

func TestGlobalOffAllowsWithoutDeciding(t *testing.T) {
	for _, path := range []string{"/builds/17/rerun", "/api/v2/publish/images"} {
		authorizer, audit := modeAuthorizer(t, "off", "enforce", denyChecker())
		response, err := authorizer.Authorize(context.Background(), attributesForPath(path))
		if err != nil {
			t.Fatalf("Authorize() error = %v", err)
		}
		if response.GetOkResponse() == nil {
			t.Fatalf("off mode denied %s", path)
		}
		if audit.events[0].WouldDeny {
			t.Fatalf("off mode recorded a would-deny for %s: %+v", path, audit.events[0])
		}
	}
}

func TestGlobalModeDefaultsToEnforce(t *testing.T) {
	authorizer, _ := modeAuthorizer(t, "", "enforce", denyChecker())
	response, err := authorizer.Authorize(context.Background(), testAttributes("token"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetDeniedResponse() == nil {
		t.Fatal("an unset global mode did not default to enforce")
	}
}

func denyChecker() Checker {
	return checkerFunc{modelID: "model-1", check: func(context.Context, string, string, string, bool) (bool, error) {
		return false, nil
	}}
}
