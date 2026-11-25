package decision

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/identity"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/resolver"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
)

type verifierFunc func(context.Context, string) (identity.Principal, error)

func (f verifierFunc) Verify(ctx context.Context, token string) (identity.Principal, error) {
	return f(ctx, token)
}

type resolverFunc struct {
	resolve func(context.Context, string, string) (string, error)
}

type contextPolicyFunc func(context.Context, PolicyContext) (bool, error)

func (f contextPolicyFunc) Evaluate(ctx context.Context, policyContext PolicyContext) (bool, error) {
	return f(ctx, policyContext)
}

func (f resolverFunc) Resolve(ctx context.Context, typ, id string) (string, error) {
	return f.resolve(ctx, typ, id)
}
func (resolverFunc) Version() string { return "fake-v1" }

type checkerFunc struct {
	check   func(context.Context, string, string, string, bool) (bool, error)
	modelID string
}

func (f checkerFunc) Check(ctx context.Context, user, object, relation string, higher bool) (bool, error) {
	return f.check(ctx, user, object, relation, higher)
}
func (f checkerFunc) ModelID() string { return f.modelID }

type recorder struct{ events []AuditEvent }

func (r *recorder) Record(_ context.Context, event AuditEvent) { r.events = append(r.events, event) }

func TestAuthorizeUsesVerifiedPrincipalAndNeverClientIdentityHeaders(t *testing.T) {
	var gotPrincipal, gotToken string
	checker := checkerFunc{modelID: "model-1", check: func(_ context.Context, user, object, relation string, higher bool) (bool, error) {
		gotPrincipal = user
		if !((object == "capability:tibuild.devbuild.rerun" && relation == "invoke") || (object == "devbuild:17" && relation == "can_rerun")) || !higher {
			t.Fatalf("unexpected check: %s#%s on %s; higher=%t", object, relation, user, higher)
		}
		return true, nil
	}}
	a := newTestAuthorizer(t, "enforce", checker, func(ctx context.Context, token string) (identity.Principal, error) {
		gotToken = token
		return identity.Principal{Subject: "user:https://issuer.example|signed-subject"}, nil
	}, testResolver(), nil)
	response, err := a.Authorize(context.Background(), testAttributes("signed-token"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetOkResponse() == nil {
		t.Fatal("valid authorization was not allowed")
	}
	if gotPrincipal != "user:https://issuer.example|signed-subject" || gotToken != "signed-token" {
		t.Fatalf("principal/token = %q/%q", gotPrincipal, gotToken)
	}
}

func TestAuthorizeDeniesInvalidIdentityInEnforceMode(t *testing.T) {
	a := newTestAuthorizer(t, "enforce", allowChecker(), func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{}, errors.New("invalid signature")
	}, nil, nil)
	response, err := a.Authorize(context.Background(), testAttributes("bad-token"))
	if err != nil || response.GetDeniedResponse() == nil {
		t.Fatalf("Authorize() = (%v, %v), want HTTP deny", response, err)
	}
}

func TestAuthorizeObserveAllowsAndAuditsWouldDeny(t *testing.T) {
	log := &recorder{}
	a := newTestAuthorizer(t, "observe", checkerFunc{modelID: "model-1", check: func(context.Context, string, string, string, bool) (bool, error) {
		return false, nil
	}}, func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, testResolver(), log)
	response, err := a.Authorize(context.Background(), testAttributes("token-must-not-be-logged"))
	if err != nil || response.GetOkResponse() == nil {
		t.Fatalf("observe response = (%v, %v), want allow", response, err)
	}
	if len(log.events) != 1 || !log.events[0].WouldDeny || log.events[0].Outcome != "allow" || log.events[0].Reason != "relationship_denied" {
		t.Fatalf("unexpected audit event: %+v", log.events)
	}
	if strings.Contains(log.events[0].Principal, "token-must-not-be-logged") {
		t.Fatal("audit event contains the bearer token")
	}
}

func TestAuthorizeObserveAllowsWhenOpenFGAIsUnavailable(t *testing.T) {
	log := &recorder{}
	a := newTestAuthorizer(t, "observe", checkerFunc{check: func(context.Context, string, string, string, bool) (bool, error) {
		return false, errors.New("backend unavailable")
	}}, func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, testResolver(), log)
	response, err := a.Authorize(context.Background(), testAttributes("token"))
	if err != nil || response.GetOkResponse() == nil || len(log.events) != 1 || !log.events[0].WouldDeny {
		t.Fatalf("observe backend failure = (%v, %v), audit=%+v", response, err, log.events)
	}
}

func TestAuthorizeOffSkipsIdentityAndOpenFGAChecks(t *testing.T) {
	a := newTestAuthorizer(t, "off", checkerFunc{check: func(context.Context, string, string, string, bool) (bool, error) {
		t.Fatal("OpenFGA called in off mode")
		return false, nil
	}}, func(context.Context, string) (identity.Principal, error) {
		t.Fatal("identity verifier called in off mode")
		return identity.Principal{}, nil
	}, nil, nil)
	response, err := a.Authorize(context.Background(), testAttributes(""))
	if err != nil || response.GetOkResponse() == nil {
		t.Fatalf("off response = (%v, %v), want allow", response, err)
	}
}

func TestAuthorizeDependencyErrorIsReturnedForFailClosedTransport(t *testing.T) {
	a := newTestAuthorizer(t, "enforce", checkerFunc{check: func(context.Context, string, string, string, bool) (bool, error) {
		return false, errors.New("backend down")
	}}, func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, nil, nil)
	if _, err := a.Authorize(context.Background(), testAttributes("signed-token")); err == nil {
		t.Fatal("Authorize() succeeded while OpenFGA is unavailable")
	}
}

func TestAuthorizeAuditsDependencyErrorsWithoutLoggingCredentials(t *testing.T) {
	log := &recorder{}
	var checks int
	checker := checkerFunc{modelID: "model-1", check: func(_ context.Context, _, object, _ string, _ bool) (bool, error) {
		checks++
		if object == "capability:tibuild.devbuild.rerun" {
			return true, nil
		}
		return false, errors.New("backend unavailable")
	}}
	a := newTestAuthorizer(t, "enforce", checker, func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, testResolver(), log)
	if _, err := a.Authorize(context.Background(), testAttributes("secret-token")); err == nil {
		t.Fatal("Authorize() succeeded during a backend error")
	}
	if len(log.events) != 1 {
		t.Fatalf("audit event count = %d, want 1", len(log.events))
	}
	event := log.events[0]
	if checks != 2 || event.Outcome != "error" || event.Reason != "openfga_error" || event.ModelID != "model-1" || event.ResourceType != "devbuild" || event.ResourceID != "17" {
		t.Fatalf("unexpected dependency audit event: %+v; checks=%d", event, checks)
	}
	if strings.Contains(event.Principal, "secret-token") {
		t.Fatal("audit event contains the credential")
	}
}

func TestContextPolicyEvaluatorReceivesOnlyVerifiedContextAndCanDeny(t *testing.T) {
	a := newTestAuthorizer(t, "enforce", allowChecker(), func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:https://issuer.example|verified"}, nil
	}, testResolver(), nil)
	a.WithContextPolicyEvaluator(contextPolicyFunc(func(_ context.Context, policyContext PolicyContext) (bool, error) {
		if policyContext.Principal != "user:https://issuer.example|verified" || policyContext.RouteID != "tibuild-machine" || policyContext.Method != "POST" || policyContext.Capability != "capability:tibuild.devbuild.rerun" || len(policyContext.Resources) != 1 || policyContext.Resources[0] != "devbuild:17" {
			t.Fatalf("unexpected normalized policy context: %+v", policyContext)
		}
		return false, nil
	}))
	response, err := a.Authorize(context.Background(), testAttributes("secret-token"))
	if err != nil || response.GetDeniedResponse() == nil {
		t.Fatalf("context policy decision = (%v,%v), want deny", response, err)
	}
}

func TestContextPolicyFailureIsFailClosedInEnforceMode(t *testing.T) {
	a := newTestAuthorizer(t, "enforce", allowChecker(), func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, testResolver(), nil)
	a.WithContextPolicyEvaluator(contextPolicyFunc(func(context.Context, PolicyContext) (bool, error) {
		return false, errors.New("policy backend unavailable")
	}))
	if _, err := a.Authorize(context.Background(), testAttributes("token")); err == nil {
		t.Fatal("context policy failure returned an allow decision")
	}
}

func TestAuthorizeRejectsClientSuppliedIdentityHeader(t *testing.T) {
	attributes := testAttributes("")
	attributes.GetRequest().GetHttp().Headers["x-user-id"] = "user:attacker"
	a := newTestAuthorizer(t, "enforce", allowChecker(), func(_ context.Context, token string) (identity.Principal, error) {
		if token == "user:attacker" {
			t.Fatal("unverified identity header was treated as a token")
		}
		return identity.Principal{}, errors.New("missing token")
	}, nil, nil)
	response, err := a.Authorize(context.Background(), attributes)
	if err != nil || response.GetDeniedResponse() == nil {
		t.Fatalf("spoofed identity request = (%v, %v), want deny", response, err)
	}
}

func TestAuthorizeRequiresTrustedRouteAndCredentialExtensions(t *testing.T) {
	deny := func(authorizer *Authorizer, attrs *authv3.AttributeContext) {
		t.Helper()
		response, err := authorizer.Authorize(context.Background(), attrs)
		if err != nil || response.GetDeniedResponse() == nil {
			t.Fatalf("Authorize() = (%v,%v), want deny", response, err)
		}
	}
	deny(NewAuthorizer(nil, nil, nil, nil, nil), testAttributes("token"))
	attrs := testAttributes("token")
	attrs.ContextExtensions[extensionIdentityMode] = ""
	deny(newTestAuthorizer(t, "enforce", allowChecker(), func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, testResolver(), nil), attrs)
}

func TestSafeCorrelationIDAcceptsOnlyUUIDs(t *testing.T) {
	valid := "123e4567-e89b-12d3-a456-426614174000"
	if got := safeCorrelationID(valid); got != valid {
		t.Fatalf("safeCorrelationID(valid) = %q", got)
	}
	for _, invalid := range []string{"req-1", "eyJhbGciOiJSUzI1NiJ9.token.signature", "123e4567e89b12d3a456426614174000"} {
		if got := safeCorrelationID(invalid); got != "" {
			t.Errorf("safeCorrelationID(%q) = %q, want empty", invalid, got)
		}
	}
}

func TestExtractTokenRequiresBearerSchemeForAuthorizationHeader(t *testing.T) {
	for _, tc := range []struct {
		value string
		want  string
	}{
		{value: "Bearer signed-token", want: "signed-token"},
		{value: "Basic signed-token"},
		{value: "Bearer two tokens"},
	} {
		got, ok := extractToken(map[string]string{"authorization": tc.value}, "Authorization")
		if tc.want == "" && ok {
			t.Errorf("extractToken(%q) accepted unsupported authorization value", tc.value)
		}
		if tc.want != "" && (!ok || got != tc.want) {
			t.Errorf("extractToken(%q) = (%q,%t), want (%q,true)", tc.value, got, ok, tc.want)
		}
	}
}

func TestAuthorizeResolvesPathResourceAndRequiresAllChecks(t *testing.T) {
	var checks int
	checker := checkerFunc{check: func(_ context.Context, _, object, relation string, _ bool) (bool, error) {
		checks++
		if object == "capability:tibuild.devbuild.rerun" {
			return true, nil
		}
		if object != "devbuild:canonical-17" || relation != "can_rerun" {
			t.Fatalf("unexpected resolved check: %s#%s", object, relation)
		}
		return false, nil
	}}
	a := newTestAuthorizer(t, "enforce", checker, func(context.Context, string) (identity.Principal, error) {
		return identity.Principal{Subject: "user:verified"}, nil
	}, resolverFunc{resolve: func(_ context.Context, typ, id string) (string, error) {
		if typ != "devbuild" || id != "17" {
			t.Fatalf("Resolve(%q,%q)", typ, id)
		}
		return "devbuild:canonical-17", nil
	}}, nil)
	response, err := a.Authorize(context.Background(), testAttributes("token"))
	if err != nil || response.GetDeniedResponse() == nil || checks != 2 {
		t.Fatalf("resource decision = (%v, %v), checks=%d", response, err, checks)
	}
}

func newTestAuthorizer(t *testing.T, mode string, checker Checker, verify func(context.Context, string) (identity.Principal, error), resolver Resolver, audit AuditSink) *Authorizer {
	t.Helper()
	data := []byte(`{"version":"test","routes":[{"id":"rerun","host":"example.test","route":"tibuild-machine","method":"POST","path":"/builds/{id}/rerun","priority":10,"mode":"` + mode + `","checks":[{"object":"capability:tibuild.devbuild.rerun","relation":"invoke"},{"object_from":"devbuild","id_from":"path.id","relation":"can_rerun"}]}]}`)
	r, err := registry.Parse(data)
	if err != nil {
		t.Fatalf("registry.Parse() error = %v", err)
	}
	return NewAuthorizer(r, map[string]Credential{"machine": {Header: "x-verified-token", Verifier: verifierFunc(verify)}}, resolver, checker, audit)
}

func allowChecker() Checker {
	return checkerFunc{check: func(context.Context, string, string, string, bool) (bool, error) { return true, nil }}
}

func testResolver() Resolver {
	return resolverFunc{resolve: func(_ context.Context, typ, id string) (string, error) {
		return typ + ":" + id, nil
	}}
}

func testAttributes(token string) *authv3.AttributeContext {
	return &authv3.AttributeContext{
		ContextExtensions: map[string]string{"route_id": "tibuild-machine", "identity_mode": "machine"},
		Request: &authv3.AttributeContext_Request{Http: &authv3.AttributeContext_HttpRequest{
			Id: "req-1", Method: "POST", Host: "example.test", Path: "/builds/17/rerun",
			Headers: map[string]string{"x-verified-token": token, "x-user-id": "user:attacker", "x-request-id": "req-1"},
		}},
	}
}

func TestRouteIdentityModeSelectsOneVerifier(t *testing.T) {
	for _, mode := range []string{"browser", "machine"} {
		t.Run(mode, func(t *testing.T) {
			a := newTestAuthorizer(t, "enforce", allowChecker(), nil, testResolver(), nil)
			headers := map[string]string{"browser": "x-forwarded-id-token", "machine": "authorization"}
			for name, header := range headers {
				a.credentials[name] = Credential{Header: header, Verifier: verifierFunc(func(_ context.Context, token string) (identity.Principal, error) {
					if name != mode {
						t.Fatalf("route selected %s but called %s verifier", mode, name)
					}
					if token != name+"-token" {
						return identity.Principal{}, errors.New("unexpected credential")
					}
					return identity.Principal{Subject: name + "-subject"}, nil
				})}
			}
			attrs := testAttributes("")
			attrs.ContextExtensions["identity_mode"] = mode
			requestHeaders := attrs.Request.Http.Headers
			requestHeaders["authorization"] = "Bearer machine-token"
			requestHeaders["x-forwarded-id-token"] = "browser-token"
			resp, err := a.Authorize(context.Background(), attrs)
			if err != nil || resp.GetOkResponse() == nil {
				t.Fatalf("expected allow: %v", err)
			}
			delete(requestHeaders, headers[mode])
			resp, err = a.Authorize(context.Background(), attrs)
			if err != nil || resp.GetDeniedResponse() == nil {
				t.Fatalf("missing selected credential must deny: %v", err)
			}
			requestHeaders[headers[mode]] = "invalid"
			if mode == "machine" {
				requestHeaders["authorization"] = "Bearer invalid"
			}
			resp, err = a.Authorize(context.Background(), attrs)
			if err != nil || resp.GetDeniedResponse() == nil {
				t.Fatalf("invalid selected credential must deny: %v", err)
			}
		})
	}
}

// TestAuthorizeUsesCanonicalResourceNotThePathParent locks the tenant-isolation
// property: a caller cannot authorize by claiming a parent in the URL. The
// resource is the resolver's canonical object, and the path's project segment
// never becomes an authorization input.
func TestAuthorizeUsesCanonicalResourceNotThePathParent(t *testing.T) {
	registryData := []byte(`{"version":"test","routes":[{"id":"nested-rerun","host":"example.test","route":"tibuild-machine","method":"POST","path":"/projects/{project}/builds/{id}/rerun","priority":10,"mode":"enforce","checks":[{"object":"capability:tibuild.devbuild.rerun","relation":"invoke"},{"object_from":"devbuild","id_from":"path.id","relation":"can_rerun"}]}]}`)
	policyRegistry, err := registry.Parse(registryData)
	if err != nil {
		t.Fatalf("registry.Parse() error = %v", err)
	}
	catalog, err := resolver.Parse([]byte(`{"version":"catalog-v1","resources":{"devbuild/123":"devbuild:canonical-123"}}`))
	if err != nil {
		t.Fatal(err)
	}
	var checked []string
	denyChecker := checkerFunc{modelID: "model-1", check: func(_ context.Context, _, object, _ string, _ bool) (bool, error) {
		checked = append(checked, object)
		return object != "devbuild:canonical-123", nil
	}}
	audit := &recorder{}
	authorizer := NewAuthorizer(policyRegistry,
		map[string]Credential{"machine": {Header: "x-verified-token", Verifier: verifierFunc(func(context.Context, string) (identity.Principal, error) {
			return identity.Principal{Subject: "user:issuer|attacker"}, nil
		})}}, catalog, denyChecker, audit)

	request := func(path string) *authv3.AttributeContext {
		return &authv3.AttributeContext{
			ContextExtensions: map[string]string{"route_id": "tibuild-machine", "identity_mode": "machine"},
			Request: &authv3.AttributeContext_Request{Http: &authv3.AttributeContext_HttpRequest{
				Id: "req-1", Method: "POST", Host: "example.test", Path: path,
				Headers: map[string]string{"x-verified-token": "token"},
			}},
		}
	}
	// The build belongs to project B; the caller claims project A in the path.
	response, err := authorizer.Authorize(context.Background(), request("/projects/A/builds/123/rerun"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetDeniedResponse() == nil {
		t.Fatal("a forged parent path authorized another project's build")
	}
	for _, object := range checked {
		if object != "capability:tibuild.devbuild.rerun" && object != "devbuild:canonical-123" {
			t.Fatalf("check used %q; the path's project segment must never reach the decision", object)
		}
	}
	if len(audit.events) != 1 || audit.events[0].Reason != "relationship_denied" {
		t.Fatalf("audit = %+v", audit.events)
	}
	if audit.events[0].ResourceType != "devbuild" || audit.events[0].ResourceID != "canonical-123" {
		t.Fatalf("audit resource = %q/%q, want the canonical object", audit.events[0].ResourceType, audit.events[0].ResourceID)
	}

	// An unknown build ID has no trusted ownership data, so it must deny rather
	// than fall back to the path.
	response, err = authorizer.Authorize(context.Background(), request("/projects/A/builds/999/rerun"))
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetDeniedResponse() == nil {
		t.Fatal("an unknown build ID was authorized")
	}
	if got := audit.events[len(audit.events)-1].Reason; got != "resource_not_found" {
		t.Fatalf("reason = %q, want a resolution failure", got)
	}
}
