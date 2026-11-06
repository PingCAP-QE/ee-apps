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
