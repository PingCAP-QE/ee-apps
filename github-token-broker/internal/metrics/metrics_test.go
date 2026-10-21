package metrics

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestRequestAndErrorCounters(t *testing.T) {
	m, err := New()
	if err != nil {
		t.Fatal(err)
	}
	m.Request("app", true)
	m.Request("pat", true)
	m.Request("", false)
	m.Error("forbidden")
	m.RateLimited()
	m.MintDuration(10 * time.Millisecond)

	recorder := httptest.NewRecorder()
	m.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := recorder.Body.String()
	for _, want := range []string{
		`github_token_broker_requests_total`,
		`result="ok",source="app"`,
		`result="ok",source="pat"`,
		`github_token_broker_errors_total`,
		`reason="forbidden"`,
		`github_token_broker_rate_limited_total`,
		`github_token_broker_mint_seconds_count`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("metrics output missing %q:\n%s", want, body)
		}
	}
}

func TestNilMetrics(t *testing.T) {
	var m *Metrics
	m.Request("app", true)
	m.Error("x")
	m.RateLimited()
	m.MintDuration(time.Second)
}

func TestHandler(t *testing.T) {
	m, err := New()
	if err != nil {
		t.Fatal(err)
	}
	m.Request("app", true)

	recorder := httptest.NewRecorder()
	m.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))

	if recorder.Code != http.StatusOK {
		t.Fatalf("unexpected status: %d", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "github_token_broker_requests_total") {
		t.Fatalf("metrics output missing counter: %s", recorder.Body.String())
	}
}
