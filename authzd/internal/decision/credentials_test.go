package decision

import (
	"context"
	"testing"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/identity"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
)

// TestParseTokenRejectsAmbiguousForwardedHeaders locks the prerequisite for
// browser identity handoff: a forwarded token header must carry exactly one
// credential. Envoy joins repeated headers with commas, so a comma means either
// an ambiguous multi-value header or a client-supplied duplicate.
func TestParseTokenRejectsAmbiguousForwardedHeaders(t *testing.T) {
	cases := map[string]struct {
		value string
		want  string
		ok    bool
	}{
		"single token":    {value: "header.payload.signature", want: "header.payload.signature", ok: true},
		"empty":           {value: ""},
		"comma separated": {value: "header.payload.signature,other.payload.signature"},
		"trailing comma":  {value: "header.payload.signature,"},
		"space inside":    {value: "header.payload signature"},
		"leading space":   {value: " header.payload.signature"},
		"trailing space":  {value: "header.payload.signature "},
		"tab inside":      {value: "header.payload\tsignature"},
		"carriage return": {value: "header.payload.signature\r"},
		"newline":         {value: "header.payload.signature\n"},
		"nul byte":        {value: "header.payload.signature\x00"},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			got, ok := parseToken(testCase.value, "x-forwarded-id-token")
			if ok != testCase.ok || got != testCase.want {
				t.Fatalf("parseToken(%q) = (%q,%t), want (%q,%t)", testCase.value, got, ok, testCase.want, testCase.ok)
			}
		})
	}
}

func TestParseTokenKeepsBearerSchemeForAuthorization(t *testing.T) {
	for _, testCase := range []struct {
		value string
		ok    bool
	}{
		{value: "Bearer header.payload.signature", ok: true},
		{value: "bearer header.payload.signature", ok: true},
		{value: "Bearer a,b", ok: false},
		{value: "Bearer header.payload.signature,other", ok: false},
		{value: "Basic header.payload.signature", ok: false},
		{value: "header.payload.signature", ok: false},
	} {
		if _, ok := parseToken(testCase.value, "Authorization"); ok != testCase.ok {
			t.Errorf("parseToken(%q, Authorization) ok = %t, want %t", testCase.value, ok, testCase.ok)
		}
	}
}

func forwardedHeaderAuthorizer(t *testing.T, headerValue string, present bool) (*Authorizer, *recorder, *authv3.AttributeContext) {
	t.Helper()
	data := []byte(`{"version":"test","routes":[{"id":"r","host":"example.test","route":"browser-route","method":"GET","path":"/builds/{id}","priority":1,"mode":"enforce","checks":[{"object":"capability:tibuild.devbuild.read","relation":"read"}]}]}`)
	policyRegistry, err := registry.Parse(data)
	if err != nil {
		t.Fatalf("registry.Parse() error = %v", err)
	}
	audit := &recorder{}
	authorizer := NewAuthorizer(policyRegistry,
		map[string]Credential{"browser": {Header: "x-forwarded-id-token", Verifier: verifierFunc(func(_ context.Context, token string) (identity.Principal, error) {
			return identity.Principal{Subject: "user:" + token}, nil
		})}}, nil, allowChecker(), audit)

	headers := map[string]string{}
	if present {
		headers["x-forwarded-id-token"] = headerValue
	}
	attrs := testAttributes("")
	attrs.Request.Http.Method = "GET"
	attrs.Request.Http.Path = "/builds/17"
	attrs.Request.Http.Headers = headers
	attrs.ContextExtensions["identity_mode"] = "browser"
	attrs.ContextExtensions["route_id"] = "browser-route"
	return authorizer, audit, attrs
}

func TestAuthorizeDeniesAmbiguousForwardedToken(t *testing.T) {
	for _, value := range []string{
		"userA-token,userB-token",
		"userA-token ",
		"userA token",
	} {
		authorizer, audit, attrs := forwardedHeaderAuthorizer(t, value, true)
		response, err := authorizer.Authorize(context.Background(), attrs)
		if err != nil {
			t.Fatalf("Authorize() error = %v", err)
		}
		if response.GetDeniedResponse() == nil {
			t.Fatalf("an ambiguous forwarded header %q was accepted", value)
		}
		if audit.events[0].Reason != "identity_invalid" {
			t.Fatalf("reason = %q, want identity_invalid", audit.events[0].Reason)
		}
	}
}

// TestAuthorizeSeparatesMissingFromInvalidIdentity keeps the three failure
// classes distinguishable in the log: Gateway authentication failures produce no
// decision, a handoff failure is identity_missing or identity_invalid, and an
// authorization denial is relationship_denied.
func TestAuthorizeSeparatesMissingFromInvalidIdentity(t *testing.T) {
	authorizer, audit, attrs := forwardedHeaderAuthorizer(t, "", false)
	response, err := authorizer.Authorize(context.Background(), attrs)
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetDeniedResponse() == nil {
		t.Fatal("a missing forwarded token was accepted")
	}
	if audit.events[0].Reason != "identity_missing" {
		t.Fatalf("reason = %q, want identity_missing", audit.events[0].Reason)
	}
}

func TestAuthorizeAcceptsSingleForwardedToken(t *testing.T) {
	authorizer, audit, attrs := forwardedHeaderAuthorizer(t, "userA-token", true)
	response, err := authorizer.Authorize(context.Background(), attrs)
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if response.GetOkResponse() == nil {
		t.Fatalf("a valid forwarded token was denied: %+v", audit.events[0])
	}
	if audit.events[0].Principal != "user:userA-token" {
		t.Fatalf("principal = %q, want the verified subject", audit.events[0].Principal)
	}
}
