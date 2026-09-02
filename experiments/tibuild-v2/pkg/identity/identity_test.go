package identity

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestMiddlewareReadsGatewayIdentity(t *testing.T) {
	handler := Middleware()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, ok := FromContext(r.Context())
		require.True(t, ok)
		assert.Equal(t, "dev@pingcap.com", user.Email)
		w.WriteHeader(http.StatusNoContent)
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(Header, " Dev@PingCAP.com ")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestMiddlewareAllowsLegacyRequestWithoutIdentity(t *testing.T) {
	handler := Middleware()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, ok := FromContext(r.Context())
		assert.False(t, ok)
		w.WriteHeader(http.StatusNoContent)
	}))
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, httptest.NewRequest(http.MethodGet, "/", nil))
	assert.Equal(t, http.StatusNoContent, res.Code)
}
