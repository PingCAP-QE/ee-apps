package authz

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/identity"
	"github.com/stretchr/testify/assert"
)

func TestDecideReadIsAlwaysAllowed(t *testing.T) {
	for _, mode := range []Mode{ModeOff, ModeObserve, ModeEnforce} {
		p := Policy{Mode: mode, AllowPrincipals: nil}
		d := p.Decide(identity.User{}, false, ActionRead)
		assert.True(t, d.Allowed, "mode %s must not block reads", mode)
	}
}

func TestDecideMutationNeedsPrincipal(t *testing.T) {
	p := Policy{Mode: ModeEnforce, AllowPrincipals: []string{"*@pingcap.com"}}
	d := p.Decide(identity.User{}, false, ActionMutate)
	assert.False(t, d.Allowed)
	assert.True(t, d.Enforced)
	assert.Equal(t, "no authenticated principal", d.Reason)
}

func TestDecideObserveDoesNotEnforce(t *testing.T) {
	p := Policy{Mode: ModeObserve, AllowPrincipals: []string{"*@pingcap.com"}}
	d := p.Decide(identity.User{}, false, ActionMutate)
	assert.False(t, d.Allowed)
	assert.False(t, d.Enforced, "observe mode records the decision without blocking")
	assert.False(t, d.Denied())
}

func TestDecideOffAllowsLegacyTraffic(t *testing.T) {
	p := Policy{Mode: ModeOff}
	d := p.Decide(identity.User{}, false, ActionMutate)
	assert.True(t, d.Allowed)
}

func TestPrincipalAllowedPatterns(t *testing.T) {
	tests := []struct {
		name     string
		allowed  []string
		email    string
		expected bool
	}{
		{"exact match", []string{"dev@pingcap.com"}, "dev@pingcap.com", true},
		{"exact match is case insensitive", []string{"Dev@PingCAP.com"}, "dev@pingcap.com", true},
		{"domain glob", []string{"*@pingcap.com"}, "anyone@pingcap.com", true},
		{"domain glob does not cross domains", []string{"*@pingcap.com"}, "someone@example.com", false},
		{"empty list allows nobody", nil, "dev@pingcap.com", false},
		{"blank pattern is ignored", []string{"  "}, "dev@pingcap.com", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p := Policy{Mode: ModeEnforce, AllowPrincipals: tc.allowed}
			got := p.principalAllowed(identity.User{Email: tc.email})
			assert.Equal(t, tc.expected, got)
		})
	}
}

func TestMiddlewareEnforcesOnMutation(t *testing.T) {
	p := Policy{Mode: ModeEnforce, AllowPrincipals: []string{"*@pingcap.com"}}
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })

	handler := identity.NewMiddleware(identity.Headers{}, identity.Policy{})(p.Middleware(next))

	req := httptest.NewRequest(http.MethodPost, "/api/v2/publish/tiup/publish-request-single", nil)
	req.Header.Set(identity.DefaultUserEmailHeader, "attacker@example.com")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)

	assert.Equal(t, http.StatusUnauthorized, res.Code, "untrusted headers leave the request without a principal")
}

func TestMiddlewareAllowsAllowlistedServicePrincipal(t *testing.T) {
	p := Policy{Mode: ModeEnforce, AllowPrincipals: []string{"tibuild-v2@pingcap.com"}}
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	handler := identity.NewMiddleware(identity.Headers{}, identity.Policy{ServiceIdentity: "tibuild-v2@pingcap.com"})(p.Middleware(next))

	res := httptest.NewRecorder()
	handler.ServeHTTP(res, httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil))
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestMiddlewareDeniesPrincipalOutsideAllowlist(t *testing.T) {
	p := Policy{Mode: ModeEnforce, AllowPrincipals: []string{"*@pingcap.com"}}
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	handler := identity.NewMiddleware(identity.Headers{}, identity.Policy{
		TrustHeaders:    true,
		ServiceIdentity: "someone@example.com",
	})(p.Middleware(next))

	req := httptest.NewRequest(http.MethodPut, "/api/v2/devbuilds/1", nil)
	req.Header.Set(identity.DefaultUserEmailHeader, "attacker@example.com")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)

	assert.Equal(t, http.StatusForbidden, res.Code)
}

func TestMiddlewareObservePassesThrough(t *testing.T) {
	p := Policy{Mode: ModeObserve, AllowPrincipals: []string{"*@pingcap.com"}}
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	handler := p.Middleware(next)

	res := httptest.NewRecorder()
	handler.ServeHTTP(res, httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil))
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestActionForMethod(t *testing.T) {
	assert.Equal(t, ActionRead, ActionForMethod(http.MethodGet))
	assert.Equal(t, ActionRead, ActionForMethod(http.MethodHead))
	assert.Equal(t, ActionMutate, ActionForMethod(http.MethodPost))
	assert.Equal(t, ActionMutate, ActionForMethod(http.MethodPut))
	assert.Equal(t, ActionMutate, ActionForMethod(http.MethodDelete))
}
