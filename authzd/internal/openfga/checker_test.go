package openfga

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	fga "github.com/openfga/go-sdk"
)

const testULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

func TestCheckerPinsModelAndRequestsHigherConsistency(t *testing.T) {
	var request map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/stores/"+testULID+"/check") {
			t.Errorf("unexpected OpenFGA request: %s %s", r.Method, r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Errorf("decode check request: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"allowed":true}`))
	}))
	defer server.Close()

	checker, err := NewChecker(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatalf("NewChecker() error = %v", err)
	}
	allowed, err := checker.Check(context.Background(), "user:issuer|sub", "devbuild:17", "can_rerun", true)
	if err != nil || !allowed {
		t.Fatalf("Check() = (%t, %v), want allow", allowed, err)
	}
	if request["authorization_model_id"] != testULID || request["consistency"] != string(fga.CONSISTENCYPREFERENCE_HIGHER_CONSISTENCY) {
		t.Fatalf("model/consistency = %v/%v", request["authorization_model_id"], request["consistency"])
	}
	if checker.ModelID() != testULID {
		t.Fatalf("ModelID() = %q", checker.ModelID())
	}
}

func TestCheckerReturnsDenyAndPropagatesBackendFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"allowed":false}`))
	}))
	checker, err := NewChecker(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatal(err)
	}
	allowed, err := checker.Check(context.Background(), "user:x", "capability:rerun", "invoke", false)
	if err != nil || allowed {
		t.Fatalf("Check() = (%t, %v), want deny", allowed, err)
	}
	server.Close()
	if _, err := checker.Check(context.Background(), "user:x", "capability:rerun", "invoke", false); err == nil {
		t.Fatal("Check() succeeded after backend closed")
	}
}

func TestCheckerRequiresPinnedStoreAndModel(t *testing.T) {
	if _, err := NewChecker(Config{APIURL: "http://localhost"}); err == nil {
		t.Fatal("NewChecker() accepted missing store/model IDs")
	}
}

func TestCheckerRejectsInvalidAPIURLAndHandlesNilReceiver(t *testing.T) {
	for _, raw := range []string{"file:///tmp/fga", "https://user:pass@fga.example", "https://fga.example?token=secret"} {
		if _, err := NewChecker(Config{APIURL: raw, StoreID: testULID, ModelID: testULID}); err == nil {
			t.Errorf("NewChecker() accepted API URL %q", raw)
		}
	}
	var checker *Checker
	if _, err := checker.Check(context.Background(), "user:x", "object:1", "read", false); err == nil {
		t.Fatal("nil Checker.Check() succeeded")
	}
	if checker.ModelID() != "" {
		t.Fatal("nil Checker returned a model ID")
	}
}

func TestCheckerRetriesTransientBackendFailureOnlyWithinBound(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) < 3 {
			http.Error(w, `{"code":"internal_error"}`, http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"allowed":true}`))
	}))
	defer server.Close()
	checker, err := NewChecker(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatal(err)
	}
	allowed, err := checker.Check(context.Background(), "user:x", "capability:rerun", "invoke", false)
	if err != nil || !allowed || attempts.Load() != 3 {
		t.Fatalf("Check() = (%t,%v), attempts=%d; want allow after exactly 2 retries", allowed, err, attempts.Load())
	}
}

func TestCheckerUsesConfiguredAPITokenAndDoesNotRetryClientErrors(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts.Add(1)
		if r.Header.Get("Authorization") != "Bearer test-secret" {
			t.Errorf("Authorization header = %q", r.Header.Get("Authorization"))
		}
		http.Error(w, `{"code":"validation_error"}`, http.StatusBadRequest)
	}))
	defer server.Close()
	checker, err := NewChecker(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID, APIToken: "test-secret"})
	if err != nil {
		t.Fatalf("NewChecker() error = %v", err)
	}
	if _, err := checker.Check(context.Background(), "user:x", "capability:rerun", "invoke", false); err == nil {
		t.Fatal("Check() succeeded after client error")
	}
	if attempts.Load() != 1 {
		t.Fatalf("client error attempts = %d, want 1", attempts.Load())
	}
}
