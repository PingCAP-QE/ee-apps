package identity

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/api/idtoken"
)

type fakeValidator struct {
	payload *idtoken.Payload
	err     error
}

func (f fakeValidator) Validate(context.Context, string, string) (*idtoken.Payload, error) {
	return f.payload, f.err
}

func TestMiddleware(t *testing.T) {
	options := Options{Audience: "client", Issuer: "https://accounts.google.com", HostedDomain: "pingcap.com", EmailDomain: "pingcap.com"}
	payload := &idtoken.Payload{Issuer: options.Issuer, Claims: map[string]any{"email": "Dev@PingCAP.com", "email_verified": true, "hd": "pingcap.com"}}
	handler := MiddlewareWithValidator(options, fakeValidator{payload: payload})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, ok := FromContext(r.Context())
		require.True(t, ok)
		assert.Equal(t, "dev@pingcap.com", user.Email)
		w.WriteHeader(http.StatusNoContent)
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(Header, "signed-token")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	assert.Equal(t, http.StatusNoContent, res.Code)
}

func TestMiddlewareRejectsUnverifiedEmail(t *testing.T) {
	payload := &idtoken.Payload{Issuer: "issuer", Claims: map[string]any{"email": "dev@pingcap.com", "email_verified": false, "hd": "pingcap.com"}}
	handler := MiddlewareWithValidator(Options{Audience: "client", Issuer: "issuer", HostedDomain: "pingcap.com", EmailDomain: "pingcap.com"}, fakeValidator{payload: payload})(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(Header, "signed-token")
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	assert.Equal(t, http.StatusUnauthorized, res.Code)
}
