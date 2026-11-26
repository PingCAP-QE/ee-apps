package openfga

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/relationships"
)

type recordedRequest struct {
	method string
	path   string
	body   map[string]any
}

func newRecordingServer(t *testing.T, handler func(recordedRequest) (int, string)) (*httptest.Server, *[]recordedRequest) {
	t.Helper()
	var (
		mu       sync.Mutex
		recorded []recordedRequest
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if r.Body != nil {
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil && err.Error() != "EOF" {
				t.Errorf("decode %s %s: %v", r.Method, r.URL.Path, err)
			}
		}
		request := recordedRequest{method: r.Method, path: r.URL.Path, body: body}
		mu.Lock()
		recorded = append(recorded, request)
		mu.Unlock()
		status, response := handler(request)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(response))
	}))
	t.Cleanup(server.Close)
	return server, &recorded
}

func callerTuple(user, object string) relationships.Tuple {
	return relationships.Tuple{User: user, Relation: "caller", Object: object}
}

func TestTupleStoreReadsEveryPageForAnObjectType(t *testing.T) {
	server, recorded := newRecordingServer(t, func(request recordedRequest) (int, string) {
		if !strings.HasSuffix(request.path, "/stores/"+testULID+"/read") {
			t.Errorf("unexpected read path %q", request.path)
		}
		// The deployed OpenFGA rejects a type-only query, so the store must read
		// unfiltered and select the object type itself.
		if tupleKey, ok := request.body["tuple_key"].(map[string]any); ok {
			if object, present := tupleKey["object"]; present && object != nil {
				t.Errorf("read sent a type-only filter (object=%v), which this OpenFGA rejects", object)
			}
		}
		if request.body["continuation_token"] == nil {
			return http.StatusOK, `{"tuples":[{"key":{"user":"team:release#member","relation":"caller","object":"capability:a"},"timestamp":"2026-11-25T09:00:00Z"},{"key":{"user":"user:google|bob","relation":"viewer","object":"project:tidb"},"timestamp":"2026-11-25T09:00:00Z"}],"continuation_token":"page-2"}`
		}
		return http.StatusOK, `{"tuples":[{"key":{"user":"user:google|alice","relation":"caller","object":"capability:b"},"timestamp":"2026-11-25T09:00:00Z"}]}`
	})

	store, err := NewTupleStore(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatalf("NewTupleStore() error = %v", err)
	}
	tuples, err := store.Read(context.Background(), "capability")
	if err != nil {
		t.Fatalf("Read() error = %v", err)
	}
	if len(tuples) != 2 || tuples[0].Object != "capability:a" || tuples[1].User != "user:google|alice" {
		t.Fatalf("Read() = %+v", tuples)
	}
	if len(*recorded) != 2 {
		t.Fatalf("read pages = %d, want 2", len(*recorded))
	}
}

func TestTupleStoreAppliesDeletesBeforeWritesInBatches(t *testing.T) {
	server, recorded := newRecordingServer(t, func(recordedRequest) (int, string) {
		return http.StatusOK, `{}`
	})
	store, err := NewTupleStore(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatal(err)
	}
	store.WithBatchSize(2)

	var change relationships.ChangeSet
	for i := 0; i < 3; i++ {
		change.Deletes = append(change.Deletes, callerTuple("team:gone#member", fmt.Sprintf("capability:delete-%d", i)))
		change.Writes = append(change.Writes, callerTuple("team:new#member", fmt.Sprintf("capability:write-%d", i)))
	}
	if err := store.Apply(context.Background(), change); err != nil {
		t.Fatalf("Apply() error = %v", err)
	}

	if len(*recorded) != 4 {
		t.Fatalf("write requests = %d, want 2 delete batches then 2 write batches", len(*recorded))
	}
	for i, request := range (*recorded)[:2] {
		if _, ok := request.body["deletes"]; !ok {
			t.Fatalf("request %d is not a delete: %+v", i, request.body)
		}
		if _, ok := request.body["writes"]; ok {
			t.Fatalf("request %d mixes deletes and writes", i)
		}
		if request.body["authorization_model_id"] != testULID {
			t.Fatalf("request %d did not pin the model: %+v", i, request.body)
		}
	}
	for i, request := range (*recorded)[2:] {
		if _, ok := request.body["writes"]; !ok {
			t.Fatalf("request %d is not a write: %+v", i, request.body)
		}
	}
	deletes, _ := (*recorded)[0].body["deletes"].(map[string]any)
	keys, _ := deletes["tuple_keys"].([]any)
	if len(keys) != 2 {
		t.Fatalf("first delete batch = %v, want the configured batch size", keys)
	}
}

func TestTupleStoreReportsBackendFailures(t *testing.T) {
	server, _ := newRecordingServer(t, func(recordedRequest) (int, string) {
		return http.StatusInternalServerError, `{"code":"internal_error","message":"boom"}`
	})
	store, err := NewTupleStore(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Read(context.Background(), "capability"); err == nil {
		t.Fatal("Read() succeeded against a failing backend")
	}
	if err := store.Apply(context.Background(), relationships.ChangeSet{Writes: []relationships.Tuple{callerTuple("team:r#member", "capability:a")}}); err == nil {
		t.Fatal("Apply() succeeded against a failing backend")
	}

	var unconfigured *TupleStore
	if _, err := unconfigured.Read(context.Background(), "capability"); err == nil {
		t.Fatal("nil TupleStore.Read() succeeded")
	}
	if err := unconfigured.Apply(context.Background(), relationships.ChangeSet{}); err == nil {
		t.Fatal("nil TupleStore.Apply() succeeded")
	}
}

func TestTupleStoreRequiresStoreAndModel(t *testing.T) {
	if _, err := NewTupleStore(Config{APIURL: "http://localhost"}); err == nil {
		t.Fatal("NewTupleStore() accepted a missing store/model ID")
	}
	server, _ := newRecordingServer(t, func(recordedRequest) (int, string) { return http.StatusOK, `{}` })
	store, err := NewTupleStore(Config{APIURL: server.URL, StoreID: testULID, ModelID: testULID})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Apply(context.Background(), relationships.ChangeSet{}); err != nil {
		t.Fatalf("Apply() with no changes = %v, want a no-op", err)
	}
}
