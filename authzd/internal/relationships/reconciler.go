// Package sync reconciles reviewed relationship tuples into OpenFGA.
//
// A reconciler owns a declared slice of the tuple store and makes that slice
// match one complete, reviewed snapshot. It never deletes tuples outside its
// scope, never treats an incomplete or unreadable snapshot as an authoritative
// empty membership list, and never writes tuples derived from unverified
// request data.
package relationships

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"
	"time"
)

// Errors returned by Reconcile. Callers use them to classify outcomes; none of
// them are retried into a mutation, because every one of them leaves the store
// untouched.
var (
	ErrInvalidSnapshot    = errors.New("snapshot metadata is invalid")
	ErrIncompleteSnapshot = errors.New("snapshot is not complete")
	ErrEmptySnapshot      = errors.New("snapshot would delete every owned tuple")
	ErrInvalidTuple       = errors.New("snapshot contains an invalid tuple")
	ErrOutOfScope         = errors.New("snapshot contains a tuple outside the owned scope")
)

// Tuple is one OpenFGA relationship tuple. The store serializes it as
// user#relation@object.
type Tuple struct {
	User     string `json:"user"`
	Relation string `json:"relation"`
	Object   string `json:"object"`
}

// Key returns a stable identity for deduplication and set comparison.
func (t Tuple) Key() string { return t.User + "#" + t.Relation + "@" + t.Object }

// ChangeSet is the minimal mutation that makes the store match the snapshot.
type ChangeSet struct {
	Writes  []Tuple
	Deletes []Tuple
}

// Snapshot is one complete read from an authoritative source.
type Snapshot struct {
	// Source names the authoritative system the snapshot came from.
	Source string `json:"source"`
	// Revision is the reviewed revision of the snapshot, for audit trails.
	Revision string `json:"revision"`
	// GeneratedAt is when the source produced the data; Reconcile reports the
	// observation lag from it.
	GeneratedAt time.Time `json:"generated_at"`
	// Complete states that the source read covered the whole scope. A false
	// value means the read failed or was truncated, so it must not prune.
	Complete bool `json:"complete"`
	// Tuples is the full desired membership of the owned scope.
	Tuples []Tuple `json:"tuples"`
}

// Source reads authoritative snapshots. Implementations must set Complete only
// when the read covered the entire scope.
type Source interface {
	Fetch(context.Context) (Snapshot, error)
}

// Store is the tuple store the reconciler keeps in sync.
type Store interface {
	// Read returns the current tuples for one object type.
	Read(ctx context.Context, objectType string) ([]Tuple, error)
	// Apply writes and deletes tuples. Deletes take effect before writes.
	Apply(ctx context.Context, change ChangeSet) error
}

// Scope declares which tuples a reconciler owns. Anything outside it is left
// alone, so one store can host several independent reconcilers and manual
// grants.
type Scope struct {
	ObjectTypes []string
	Relations   []string
}

// Owns reports whether the tuple is inside the reconciler's declared slice.
func (s Scope) Owns(tuple Tuple) bool {
	objectType, _, found := strings.Cut(tuple.Object, ":")
	if !found {
		return false
	}
	return slices.Contains(s.ObjectTypes, objectType) && slices.Contains(s.Relations, tuple.Relation)
}

// Result summarizes one reconcile attempt.
type Result struct {
	Source   string
	Revision string
	// Desired is the number of deduplicated tuples in the snapshot.
	Desired int
	Writes  int
	Deletes int
	// Lag is how long before the reconciliation the source produced the data.
	Lag time.Duration
	// Applied reports whether the store was mutated.
	Applied bool
	// Planned reports that a dry run computed changes without applying them.
	Planned bool
}

// Reporter receives every reconcile outcome, successful or not.
type Reporter interface {
	Report(Result, error)
}

// Reconciler makes its scope in a Store match authoritative snapshots.
type Reconciler struct {
	source     Source
	store      Store
	scope      Scope
	now        func() time.Time
	reporters  []Reporter
	allowEmpty bool
	dryRun     bool
}

// NewReconciler validates the collaborators. A reconciler needs an explicit
// scope: an implicit global scope would let a source prune tuples it does not
// own.
func NewReconciler(source Source, store Store, scope Scope) (*Reconciler, error) {
	if source == nil {
		return nil, errors.New("relationship source is required")
	}
	if store == nil {
		return nil, errors.New("tuple store is required")
	}
	if len(scope.ObjectTypes) == 0 {
		return nil, errors.New("reconcile scope requires at least one object type")
	}
	if len(scope.Relations) == 0 {
		return nil, errors.New("reconcile scope requires at least one relation")
	}
	return &Reconciler{
		source: source, store: store, scope: scope, now: time.Now,
	}, nil
}

// WithClock overrides the clock used to compute observation lag.
func (r *Reconciler) WithClock(now func() time.Time) *Reconciler {
	if now != nil {
		r.now = now
	}
	return r
}

// WithReporter registers an outcome reporter.
func (r *Reconciler) WithReporter(reporter Reporter) *Reconciler {
	if reporter != nil {
		r.reporters = append(r.reporters, reporter)
	}
	return r
}

// WithEmptySnapshotAllowed permits a reviewed snapshot to delete every owned
// tuple. Operators set this only after confirming the source is authoritative
// empty, not failed.
func (r *Reconciler) WithEmptySnapshotAllowed(allow bool) *Reconciler {
	r.allowEmpty = allow
	return r
}

// WithDryRun computes and reports the plan without mutating the store.
func (r *Reconciler) WithDryRun(dryRun bool) *Reconciler {
	r.dryRun = dryRun
	return r
}

// Reconcile fetches one snapshot and applies the diff to the store.
func (r *Reconciler) Reconcile(ctx context.Context) (Result, error) {
	snapshot, err := r.source.Fetch(ctx)
	if err != nil {
		result := Result{}
		r.report(result, err)
		return result, fmt.Errorf("read relationship snapshot: %w", err)
	}
	result := Result{Source: snapshot.Source, Revision: snapshot.Revision}
	if err := validateSnapshot(snapshot); err != nil {
		r.report(result, err)
		return result, err
	}
	if !snapshot.GeneratedAt.IsZero() {
		result.Lag = r.now().Sub(snapshot.GeneratedAt)
	}
	if !snapshot.Complete {
		r.report(result, ErrIncompleteSnapshot)
		return result, ErrIncompleteSnapshot
	}
	desired, err := r.desired(snapshot.Tuples)
	if err != nil {
		r.report(result, err)
		return result, err
	}
	result.Desired = len(desired)
	current, err := r.current(ctx)
	if err != nil {
		r.report(result, err)
		return result, fmt.Errorf("read current relationship tuples: %w", err)
	}
	change := diff(current, desired)
	if len(change.Writes) == 0 && len(change.Deletes) == 0 {
		r.report(result, nil)
		return result, nil
	}
	if len(desired) == 0 && !r.allowEmpty {
		r.report(result, ErrEmptySnapshot)
		return result, ErrEmptySnapshot
	}
	result.Writes, result.Deletes = len(change.Writes), len(change.Deletes)
	if r.dryRun {
		result.Planned = true
		r.report(result, nil)
		return result, nil
	}
	if err := r.store.Apply(ctx, change); err != nil {
		r.report(result, err)
		return result, fmt.Errorf("apply relationship changes: %w", err)
	}
	result.Applied = true
	r.report(result, nil)
	return result, nil
}

func (r *Reconciler) desired(tuples []Tuple) ([]Tuple, error) {
	seen := make(map[string]struct{}, len(tuples))
	desired := make([]Tuple, 0, len(tuples))
	for _, tuple := range tuples {
		if err := ValidateTuple(tuple); err != nil {
			return nil, fmt.Errorf("%w: %v", ErrInvalidTuple, err)
		}
		if !r.scope.Owns(tuple) {
			return nil, fmt.Errorf("%w: %s", ErrOutOfScope, tuple.Key())
		}
		if _, dup := seen[tuple.Key()]; dup {
			continue
		}
		seen[tuple.Key()] = struct{}{}
		desired = append(desired, tuple)
	}
	return desired, nil
}

func (r *Reconciler) current(ctx context.Context) ([]Tuple, error) {
	var current []Tuple
	for _, objectType := range r.scope.ObjectTypes {
		read, err := r.store.Read(ctx, objectType)
		if err != nil {
			return nil, err
		}
		for _, tuple := range read {
			if r.scope.Owns(tuple) {
				current = append(current, tuple)
			}
		}
	}
	return current, nil
}

func (r *Reconciler) report(result Result, err error) {
	for _, reporter := range r.reporters {
		reporter.Report(result, err)
	}
}

// diff computes the writes and deletes that turn current into desired.
func diff(current, desired []Tuple) ChangeSet {
	have := make(map[string]struct{}, len(current))
	for _, tuple := range current {
		have[tuple.Key()] = struct{}{}
	}
	want := make(map[string]struct{}, len(desired))
	var change ChangeSet
	for _, tuple := range desired {
		want[tuple.Key()] = struct{}{}
		if _, ok := have[tuple.Key()]; !ok {
			change.Writes = append(change.Writes, tuple)
		}
	}
	for _, tuple := range current {
		if _, ok := want[tuple.Key()]; !ok {
			change.Deletes = append(change.Deletes, tuple)
		}
	}
	sortTuples(change.Writes)
	sortTuples(change.Deletes)
	return change
}

func sortTuples(tuples []Tuple) {
	sort.Slice(tuples, func(i, j int) bool { return tuples[i].Key() < tuples[j].Key() })
}

func validateSnapshot(snapshot Snapshot) error {
	if strings.TrimSpace(snapshot.Source) == "" {
		return fmt.Errorf("%w: source is required", ErrInvalidSnapshot)
	}
	if strings.TrimSpace(snapshot.Revision) == "" {
		return fmt.Errorf("%w: revision is required", ErrInvalidSnapshot)
	}
	if snapshot.GeneratedAt.IsZero() {
		return fmt.Errorf("%w: generated_at is required", ErrInvalidSnapshot)
	}
	return nil
}

// Outcome classifies a reconcile attempt for metrics and alerting.
func Outcome(result Result, err error) string {
	switch {
	case errors.Is(err, ErrIncompleteSnapshot):
		return "incomplete_snapshot"
	case errors.Is(err, ErrEmptySnapshot):
		return "empty_snapshot_refused"
	case errors.Is(err, ErrInvalidSnapshot):
		return "invalid_snapshot"
	case errors.Is(err, ErrInvalidTuple):
		return "invalid_tuple"
	case errors.Is(err, ErrOutOfScope):
		return "out_of_scope_tuple"
	case err != nil:
		return "error"
	case result.Planned:
		return "planned"
	case result.Applied:
		return "applied"
	default:
		return "noop"
	}
}
