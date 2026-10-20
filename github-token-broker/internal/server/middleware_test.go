package server

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authn"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/ratelimit"
)

type fakeReviewer struct {
	caller authz.Caller
	err    error
}

func (f *fakeReviewer) Review(context.Context, string) (authz.Caller, error) {
	return f.caller, f.err
}

func TestRequiresBearerToken(t *testing.T) {
	middleware := NewMiddleware(&fakeReviewer{err: authn.ErrUnauthenticated}, nil, nil, nil, nil)
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	for _, header := range []string{"", "Basic abc"} {
		request := httptest.NewRequest(http.MethodPost, "/v1/token", nil)
		if header != "" {
			request.Header.Set("Authorization", header)
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnauthorized {
			t.Fatalf("unexpected status for %q: %d", header, recorder.Code)
		}
	}
}

func TestRateLimit(t *testing.T) {
	middleware := NewMiddleware(
		&fakeReviewer{caller: authz.Caller{Namespace: "ci", ServiceAccount: "checkout"}},
		ratelimit.New(1),
		config.StaticProvider{Cfg: &config.Config{RateLimit: config.RateLimit{PerMinute: 1}}},
		nil,
		nil,
	)
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	request := func() int {
		req := httptest.NewRequest(http.MethodPost, "/v1/token", nil)
		req.Header.Set("Authorization", "Bearer token")
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, req)
		return recorder.Code
	}
	if status := request(); status != http.StatusOK {
		t.Fatalf("unexpected first status: %d", status)
	}
	if status := request(); status != http.StatusTooManyRequests {
		t.Fatalf("unexpected second status: %d", status)
	}
}

func TestPassesCallerContext(t *testing.T) {
	middleware := NewMiddleware(
		&fakeReviewer{caller: authz.Caller{Namespace: "ci", ServiceAccount: "checkout"}},
		nil,
		nil,
		nil,
		nil,
	)
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		caller, ok := authn.CallerFromContext(r.Context())
		if !ok || caller.Namespace != "ci" || caller.ServiceAccount != "checkout" {
			t.Errorf("unexpected caller: %#v", caller)
		}
		w.WriteHeader(http.StatusOK)
	}))

	request := httptest.NewRequest(http.MethodPost, "/v1/token", nil)
	request.Header.Set("Authorization", "Bearer token")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("unexpected status: %d", recorder.Code)
	}
}

func TestSkipsNonTokenPaths(t *testing.T) {
	middleware := NewMiddleware(&fakeReviewer{err: authn.ErrUnauthenticated}, nil, nil, nil, nil)
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("unexpected status: %d", recorder.Code)
	}
}

func TestAuthenticationBackendFailure(t *testing.T) {
	middleware := NewMiddleware(&fakeReviewer{err: errors.New("boom")}, nil, nil, nil, nil)
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	request := httptest.NewRequest(http.MethodPost, "/v1/token", nil)
	request.Header.Set("Authorization", "Bearer token")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("unexpected status: %d", recorder.Code)
	}
}

func TestErrorReason(t *testing.T) {
	cases := map[int]string{
		http.StatusBadRequest:          "bad_request",
		http.StatusUnauthorized:        "unauthenticated",
		http.StatusForbidden:           "forbidden",
		http.StatusNotFound:            "not_installed",
		http.StatusTooManyRequests:     "rate_limited",
		http.StatusGatewayTimeout:      "timeout",
		http.StatusBadGateway:          "upstream",
		http.StatusInternalServerError: "internal",
	}
	for status, want := range cases {
		if got := errorReason(status); got != want {
			t.Fatalf("errorReason(%d) = %q, want %q", status, got, want)
		}
	}
}
