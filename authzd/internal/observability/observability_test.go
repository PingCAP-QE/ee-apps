package observability

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/decision"
)

func TestRecorderLogsOnlyStructuredDecisionFieldsAndExposesPrivateProbes(t *testing.T) {
	var output strings.Builder
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	recorder := NewRecorder(logger)
	recorder.Record(context.Background(), decision.AuditEvent{
		DecisionID: "id-1", Principal: "user:issuer|sub", Capability: "capability:build#rerun",
		Outcome: "allow", Reason: "authorized", ModelID: "model-1", RegistryVersion: "registry-1",
		ResolverVersion: "catalog-1", CorrelationID: "request-1",
	})
	recorder.Record(context.Background(), decision.AuditEvent{Outcome: "error", Reason: "openfga_error"})
	if strings.Contains(output.String(), "secret-token") || strings.Contains(output.String(), "Bearer ") {
		t.Fatalf("audit output contains sensitive credential material: %s", output.String())
	}

	handler := recorder.Handler(func() bool { return false })
	for _, tc := range []struct {
		path string
		want int
	}{
		{path: "/healthz", want: http.StatusOK},
		{path: "/readyz", want: http.StatusServiceUnavailable},
		{path: "/metrics", want: http.StatusOK},
		{path: "/missing", want: http.StatusNotFound},
	} {
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, tc.path, nil))
		if rr.Code != tc.want {
			t.Errorf("%s status = %d, want %d", tc.path, rr.Code, tc.want)
		}
		if tc.path == "/metrics" && !strings.Contains(rr.Body.String(), `authzd_decisions_total{outcome="error"} 1`) {
			t.Errorf("error decision metric missing: %s", rr.Body.String())
		}
	}
	ready := httptest.NewRecorder()
	handler.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if ready.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil readiness callback status = %d, want unavailable", ready.Code)
	}
	readyHandler := recorder.Handler(func() bool { return true })
	ready = httptest.NewRecorder()
	readyHandler.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if ready.Code != http.StatusOK {
		t.Fatalf("ready status = %d, want OK", ready.Code)
	}
}
