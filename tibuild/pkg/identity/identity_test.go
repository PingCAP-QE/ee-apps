package identity

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestMiddlewareReadsGatewayIdentity(t *testing.T) {
	handler := Middleware(Headers{})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, ok := FromContext(r.Context())
		require.True(t, ok)
		assert.Equal(t, "user-123", user.ID)
		assert.Equal(t, "dev@pingcap.com", user.Email)
		assert.Equal(t, "Dev User", user.Name)
		w.WriteHeader(http.StatusNoContent)
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(DefaultUserIDHeader, " user-123 ")
	req.Header.Set(DefaultUserEmailHeader, " Dev@PingCAP.com ")
	req.Header.Set(DefaultUserNameHeader, " Dev User ")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestMiddlewareAllowsLegacyRequestWithoutIdentity(t *testing.T) {
	handler := Middleware(Headers{})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, ok := FromContext(r.Context())
		assert.False(t, ok)
		w.WriteHeader(http.StatusNoContent)
	}))
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, httptest.NewRequest(http.MethodGet, "/", nil))
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestMiddlewareUsesConfiguredHeaderNames(t *testing.T) {
	handler := Middleware(Headers{UserID: "X-Actor-ID", UserEmail: "X-Actor-Email", UserName: "X-Actor-Name"})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, ok := FromContext(r.Context())
		require.True(t, ok)
		assert.Equal(t, User{ID: "42", Email: "actor@example.com", Name: "Actor"}, user)
		w.WriteHeader(http.StatusNoContent)
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Actor-ID", "42")
	req.Header.Set("X-Actor-Email", "actor@example.com")
	req.Header.Set("X-Actor-Name", "Actor")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestNewMiddlewareDoesNotTrustHeadersByDefault(t *testing.T) {
	var principal User
	var ok bool
	handler := NewMiddleware(Headers{}, Policy{})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, ok = FromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil)
	req.Header.Set(DefaultUserEmailHeader, "attacker@example.com")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)

	assert.False(t, ok, "client-supplied headers must not become a principal")
	assert.Empty(t, principal.Email)
}

func TestNewMiddlewareKeepsAssertedHeadersForAudit(t *testing.T) {
	var asserted User
	var assertedOK bool
	handler := NewMiddleware(Headers{}, Policy{})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asserted, assertedOK = AssertedFromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil)
	req.Header.Set(DefaultUserEmailHeader, "Attacker@Example.com")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	assert.True(t, assertedOK)
	assert.Equal(t, "attacker@example.com", asserted.Email)
}

func TestNewMiddlewareFallsBackToServiceIdentity(t *testing.T) {
	var principal User
	var ok bool
	handler := NewMiddleware(Headers{}, Policy{ServiceIdentity: "tibuild-v2@pingcap.com", ServiceName: "tibuild-v2"})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, ok = FromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil))

	assert.True(t, ok)
	assert.Equal(t, "tibuild-v2@pingcap.com", principal.Email)
	assert.True(t, principal.Service)
}

func TestNewMiddlewareTrustsHeadersWhenEnabled(t *testing.T) {
	var principal User
	handler := NewMiddleware(Headers{}, Policy{TrustHeaders: true, ServiceIdentity: "tibuild-v2@pingcap.com"})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, _ = FromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil)
	req.Header.Set(DefaultUserEmailHeader, "Dev@PingCAP.com")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	assert.Equal(t, "dev@pingcap.com", principal.Email)
	assert.False(t, principal.Service, "a trusted header is a person, not the service account")
}

func TestNewMiddlewarePrefersServiceIdentityOverUntrustedHeader(t *testing.T) {
	var principal User
	handler := NewMiddleware(Headers{}, Policy{ServiceIdentity: "tibuild-v2@pingcap.com"})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, _ = FromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/v2/devbuilds", nil)
	req.Header.Set(DefaultUserEmailHeader, "attacker@example.com")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	assert.Equal(t, "tibuild-v2@pingcap.com", principal.Email)
	assert.True(t, principal.Service)
}
