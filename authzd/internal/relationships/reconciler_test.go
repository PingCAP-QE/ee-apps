package relationships

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

var fixedNow = time.Date(2026, time.November, 25, 9, 0, 0, 0, time.UTC)

type fakeSource struct {
	snapshot Snapshot
	err      error
	calls    int
}

func (s *fakeSource) Fetch(context.Context) (Snapshot, error) {
	s.calls++
	if s.err != nil {
		return Snapshot{}, s.err
	}
	return s.snapshot, nil
}

type fakeStore struct {
	tuples  map[string]Tuple
	applied []ChangeSet
	err     error
	readErr error
}

func tupleKey(tuple Tuple) string {
	return tuple.User + "#" + tuple.Relation + "@" + tuple.Object
}

func newFakeStore(tuples ...Tuple) *fakeStore {
	store := &fakeStore{tuples: map[string]Tuple{}}
	for _, tuple := range tuples {
		store.tuples[tupleKey(tuple)] = tuple
	}
	return store
}

func (s *fakeStore) Read(_ context.Context, objectType string) ([]Tuple, error) {
	if s.readErr != nil {
		return nil, s.readErr
	}
	var found []Tuple
	for _, tuple := range s.tuples {
		if strings.HasPrefix(tuple.Object, objectType+":") {
			found = append(found, tuple)
		}
	}
	return found, nil
}

func (s *fakeStore) Apply(_ context.Context, change ChangeSet) error {
	if s.err != nil {
		return s.err
	}
	s.applied = append(s.applied, change)
	for _, tuple := range change.Deletes {
		delete(s.tuples, tupleKey(tuple))
	}
	for _, tuple := range change.Writes {
		s.tuples[tupleKey(tuple)] = tuple
	}
	return nil
}

func (s *fakeStore) has(tuple Tuple) bool {
	_, ok := s.tuples[tupleKey(tuple)]
	return ok
}

func reviewerScope() Scope {
	return Scope{ObjectTypes: []string{"capability"}, Relations: []string{"caller"}}
}

func snapshotAt(revision string, tuples ...Tuple) Snapshot {
	return Snapshot{
		Source: "reviewed-grants", Revision: revision,
		GeneratedAt: fixedNow.Add(-time.Minute), Complete: true, Tuples: tuples,
	}
}

func caller(user, object string) Tuple {
	return Tuple{User: user, Relation: "caller", Object: object}
}

func TestReconcileBootstrapsThenIsIdempotent(t *testing.T) {
	source := &fakeSource{snapshot: snapshotAt("r1",
		caller("team:release#member", "capability:tibuild.devbuild.rerun"),
		caller("user:google|alice", "capability:tibuild.devbuild.rerun"),
	)}
	store := newFakeStore()
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatalf("NewReconciler() error = %v", err)
	}
	reconciler.WithClock(func() time.Time { return fixedNow })

	result, err := reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if !result.Applied || result.Writes != 2 || result.Deletes != 0 || result.Desired != 2 {
		t.Fatalf("first Reconcile() = %+v", result)
	}
	if result.Lag != time.Minute {
		t.Fatalf("Lag = %v, want 1m", result.Lag)
	}

	source.snapshot.Revision = "r2"
	result, err = reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("second Reconcile() error = %v", err)
	}
	if result.Applied || result.Writes != 0 || result.Deletes != 0 {
		t.Fatalf("second Reconcile() = %+v, want no-op", result)
	}
	if len(store.applied) != 1 {
		t.Fatalf("Apply() calls = %d, want 1", len(store.applied))
	}
}

func TestReconcileDeletesRevokedGrantsAndRepairsDrift(t *testing.T) {
	store := newFakeStore(
		caller("team:release#member", "capability:tibuild.devbuild.rerun"),
		caller("team:release#member", "capability:tibuild.devbuild.read"),
	)
	source := &fakeSource{snapshot: snapshotAt("r2",
		caller("team:release#member", "capability:tibuild.devbuild.read"),
		caller("user:google|bob", "capability:tibuild.devbuild.read"),
	)}
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	result, err := reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if !result.Applied || result.Writes != 1 || result.Deletes != 1 {
		t.Fatalf("Reconcile() = %+v, want 1 write and 1 delete", result)
	}
	change := store.applied[0]
	if len(change.Deletes) != 1 || change.Deletes[0].Object != "capability:tibuild.devbuild.rerun" {
		t.Fatalf("Deletes = %+v", change.Deletes)
	}
	if len(change.Writes) != 1 || change.Writes[0].User != "user:google|bob" {
		t.Fatalf("Writes = %+v", change.Writes)
	}
	// A revoked grant must stop authorizing: the store no longer holds it.
	if store.has(caller("team:release#member", "capability:tibuild.devbuild.rerun")) {
		t.Fatal("revoked grant is still stored")
	}
	if !store.has(caller("user:google|bob", "capability:tibuild.devbuild.read")) ||
		!store.has(caller("team:release#member", "capability:tibuild.devbuild.read")) || len(store.tuples) != 2 {
		t.Fatalf("store tuples after reconcile = %+v", store.tuples)
	}
}

func TestReconcileRefusesIncompleteSnapshot(t *testing.T) {
	store := newFakeStore(caller("team:release#member", "capability:tibuild.devbuild.rerun"))
	snapshot := snapshotAt("r2")
	snapshot.Complete = false
	source := &fakeSource{snapshot: snapshot}
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	_, err = reconciler.Reconcile(context.Background())
	if !errors.Is(err, ErrIncompleteSnapshot) {
		t.Fatalf("Reconcile() error = %v, want ErrIncompleteSnapshot", err)
	}
	if len(store.applied) != 0 {
		t.Fatal("an incomplete snapshot mutated the store")
	}
}

func TestReconcileRefusesAuthoritativeEmptyWithoutOptIn(t *testing.T) {
	store := newFakeStore(caller("team:release#member", "capability:tibuild.devbuild.rerun"))
	source := &fakeSource{snapshot: snapshotAt("r2")}
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	_, err = reconciler.Reconcile(context.Background())
	if !errors.Is(err, ErrEmptySnapshot) {
		t.Fatalf("Reconcile() error = %v, want ErrEmptySnapshot", err)
	}
	if len(store.applied) != 0 {
		t.Fatal("an empty snapshot pruned owned tuples without opt-in")
	}

	reconciler.WithEmptySnapshotAllowed(true)
	result, err := reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() with opt-in error = %v", err)
	}
	if !result.Applied || result.Deletes != 1 {
		t.Fatalf("Reconcile() = %+v, want the reviewed removal", result)
	}
}

func TestReconcileRejectsOutOfScopeAndMalformedTuples(t *testing.T) {
	cases := map[string]struct {
		tuple Tuple
		want  error
	}{
		"foreign object type": {
			tuple: caller("team:release#member", "devbuild:17"),
			want:  ErrOutOfScope,
		},
		"foreign relation": {
			tuple: Tuple{User: "team:release#member", Relation: "editor", Object: "capability:tibuild.devbuild.rerun"},
			want:  ErrOutOfScope,
		},
		"malformed user": {
			tuple: caller("release", "capability:tibuild.devbuild.rerun"),
			want:  ErrInvalidTuple,
		},
		"whitespace in object": {
			tuple: caller("team:release#member", "capability:tibuild devbuild"),
			want:  ErrInvalidTuple,
		},
		"empty relation": {
			tuple: Tuple{User: "team:release#member", Object: "capability:tibuild.devbuild.rerun"},
			want:  ErrInvalidTuple,
		},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			store := newFakeStore()
			source := &fakeSource{snapshot: snapshotAt("r1", testCase.tuple)}
			reconciler, err := NewReconciler(source, store, reviewerScope())
			if err != nil {
				t.Fatal(err)
			}
			if _, err := reconciler.Reconcile(context.Background()); !errors.Is(err, testCase.want) {
				t.Fatalf("Reconcile() error = %v, want %v", err, testCase.want)
			}
			if len(store.applied) != 0 {
				t.Fatal("a rejected snapshot mutated the store")
			}
		})
	}
}

func TestReconcileIgnoresTuplesOutsideTheOwnedScope(t *testing.T) {
	// The store may hold tuples owned by other reconcilers or by humans.
	store := newFakeStore(
		Tuple{User: "team:release#member", Relation: "editor", Object: "capability:tibuild.devbuild.rerun"},
		Tuple{User: "user:google|alice", Relation: "caller", Object: "project:tidb"},
		caller("team:release#member", "capability:tibuild.devbuild.rerun"),
	)
	source := &fakeSource{snapshot: snapshotAt("r2", caller("user:google|alice", "capability:tibuild.devbuild.rerun"))}
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	result, err := reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if result.Writes != 1 || result.Deletes != 1 {
		t.Fatalf("Reconcile() = %+v, want exactly one owned write and delete", result)
	}
	change := store.applied[0]
	for _, tuple := range append(append([]Tuple{}, change.Writes...), change.Deletes...) {
		if !reviewerScope().Owns(tuple) {
			t.Fatalf("reconciler touched a tuple it does not own: %+v", tuple)
		}
	}
}

func TestReconcileValidatesSnapshotMetadata(t *testing.T) {
	cases := map[string]Snapshot{
		"missing source":     {Revision: "r1", GeneratedAt: fixedNow, Complete: true, Tuples: []Tuple{caller("team:r#member", "capability:c")}},
		"missing revision":   {Source: "reviewed-grants", GeneratedAt: fixedNow, Complete: true, Tuples: []Tuple{caller("team:r#member", "capability:c")}},
		"missing generated":  {Source: "reviewed-grants", Revision: "r1", Complete: true, Tuples: []Tuple{caller("team:r#member", "capability:c")}},
		"unnamed source set": {Complete: true},
	}
	for name, snapshot := range cases {
		t.Run(name, func(t *testing.T) {
			reconciler, err := NewReconciler(&fakeSource{snapshot: snapshot}, newFakeStore(), reviewerScope())
			if err != nil {
				t.Fatal(err)
			}
			if _, err := reconciler.Reconcile(context.Background()); !errors.Is(err, ErrInvalidSnapshot) {
				t.Fatalf("Reconcile() error = %v, want ErrInvalidSnapshot", err)
			}
		})
	}
}

func TestReconcileDeduplicatesDesiredTuples(t *testing.T) {
	source := &fakeSource{snapshot: snapshotAt("r1",
		caller("team:release#member", "capability:tibuild.devbuild.rerun"),
		caller("team:release#member", "capability:tibuild.devbuild.rerun"),
	)}
	store := newFakeStore()
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	result, err := reconciler.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if result.Desired != 1 || result.Writes != 1 {
		t.Fatalf("Reconcile() = %+v, want one deduplicated tuple", result)
	}
}

func TestReconcilePropagatesSourceAndStoreFailures(t *testing.T) {
	sourceErr := errors.New("roster read failed")
	reconciler, err := NewReconciler(&fakeSource{err: sourceErr}, newFakeStore(), reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(context.Background()); !errors.Is(err, sourceErr) {
		t.Fatalf("Reconcile() error = %v, want the source error", err)
	}

	readErr := errors.New("openfga read failed")
	store := newFakeStore()
	store.readErr = readErr
	reconciler, err = NewReconciler(&fakeSource{snapshot: snapshotAt("r1", caller("team:r#member", "capability:c"))}, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(context.Background()); !errors.Is(err, readErr) {
		t.Fatalf("Reconcile() error = %v, want the read error", err)
	}

	applyErr := errors.New("openfga write failed")
	store = newFakeStore()
	store.err = applyErr
	reconciler, err = NewReconciler(&fakeSource{snapshot: snapshotAt("r1", caller("team:r#member", "capability:c"))}, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(context.Background()); !errors.Is(err, applyErr) {
		t.Fatalf("Reconcile() error = %v, want the write error", err)
	}
}

func TestNewReconcilerRequiresSourceStoreAndScope(t *testing.T) {
	if _, err := NewReconciler(nil, newFakeStore(), reviewerScope()); err == nil {
		t.Fatal("NewReconciler() accepted a nil source")
	}
	if _, err := NewReconciler(&fakeSource{}, nil, reviewerScope()); err == nil {
		t.Fatal("NewReconciler() accepted a nil store")
	}
	if _, err := NewReconciler(&fakeSource{}, newFakeStore(), Scope{Relations: []string{"caller"}}); err == nil {
		t.Fatal("NewReconciler() accepted a scope without object types")
	}
	if _, err := NewReconciler(&fakeSource{}, newFakeStore(), Scope{ObjectTypes: []string{"capability"}}); err == nil {
		t.Fatal("NewReconciler() accepted a scope without relations")
	}
}

type recordingReporter struct {
	results []Result
	errs    []error
}

func (r *recordingReporter) Report(result Result, err error) {
	r.results = append(r.results, result)
	r.errs = append(r.errs, err)
}

func TestReconcileReportsEveryOutcome(t *testing.T) {
	reporter := &recordingReporter{}
	source := &fakeSource{snapshot: snapshotAt("r1", caller("team:r#member", "capability:c"))}
	reconciler, err := NewReconciler(source, newFakeStore(), reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	reconciler.WithReporter(reporter).WithClock(func() time.Time { return fixedNow })
	if _, err := reconciler.Reconcile(context.Background()); err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if len(reporter.results) != 1 || reporter.errs[0] != nil || !reporter.results[0].Applied {
		t.Fatalf("reporter results = %+v, errs = %+v", reporter.results, reporter.errs)
	}

	source.err = errors.New("source unavailable")
	if _, err := reconciler.Reconcile(context.Background()); err == nil {
		t.Fatal("Reconcile() succeeded with a failing source")
	}
	if len(reporter.results) != 2 || reporter.errs[1] == nil {
		t.Fatalf("reporter did not record the failure: %+v", reporter.errs)
	}
}

func TestResultOutcomeLabels(t *testing.T) {
	cases := map[string]struct {
		result  Result
		err     error
		outcome string
	}{
		"applied":            {result: Result{Applied: true, Writes: 1}, outcome: "applied"},
		"noop":               {result: Result{}, outcome: "noop"},
		"incomplete":         {err: ErrIncompleteSnapshot, outcome: "incomplete_snapshot"},
		"empty":              {err: ErrEmptySnapshot, outcome: "empty_snapshot_refused"},
		"invalid snapshot":   {err: ErrInvalidSnapshot, outcome: "invalid_snapshot"},
		"invalid tuple":      {err: ErrInvalidTuple, outcome: "invalid_tuple"},
		"out of scope":       {err: ErrOutOfScope, outcome: "out_of_scope_tuple"},
		"unexpected failure": {err: errors.New("boom"), outcome: "error"},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			if got := Outcome(testCase.result, testCase.err); got != testCase.outcome {
				t.Fatalf("Outcome() = %q, want %q", got, testCase.outcome)
			}
		})
	}
}

func TestReconcileDryRunReportsPlanWithoutMutating(t *testing.T) {
	store := newFakeStore(caller("team:gone#member", "capability:tibuild.devbuild.rerun"))
	source := &fakeSource{snapshot: snapshotAt("r2", caller("team:release#member", "capability:tibuild.devbuild.rerun"))}
	reconciler, err := NewReconciler(source, store, reviewerScope())
	if err != nil {
		t.Fatal(err)
	}
	result, err := reconciler.WithDryRun(true).Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if result.Applied || !result.Planned || result.Writes != 1 || result.Deletes != 1 {
		t.Fatalf("Reconcile() = %+v, want a plan only", result)
	}
	if len(store.applied) != 0 {
		t.Fatal("dry-run mutated the store")
	}
	if got := Outcome(result, nil); got != "planned" {
		t.Fatalf("Outcome() = %q, want planned", got)
	}
}
