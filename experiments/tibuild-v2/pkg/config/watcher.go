package config

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/rs/zerolog"
	"gopkg.in/yaml.v3"
)

// DefaultPollInterval is how often the watcher re-reads the configuration file.
// Kubernetes swaps Secret mounts by replacing the ..data symlink, which does not
// produce a reliable inotify event on the mounted file, so the watcher polls the
// file content instead of watching it.
const DefaultPollInterval = 15 * time.Second

// Reload outcomes, reported by ReloadResult.Result and the config_reload_total
// metric. Rejected documents never replace the last-known-good snapshot.
const (
	// ReloadSuccess means a new snapshot was validated and published.
	ReloadSuccess = "success"
	// ReloadUnchanged means the file content hash is identical to the applied one.
	ReloadUnchanged = "unchanged"
	// ReloadInvalid means the document did not parse or failed validation.
	ReloadInvalid = "invalid"
	// ReloadRestartRequired means a field that only a restart can apply changed;
	// the whole document is rejected to avoid a half-applied configuration.
	ReloadRestartRequired = "restart_required"
	// ReloadReadError means the file could not be read (a swap window or a
	// broken mount). It is retried on the next poll.
	ReloadReadError = "read_error"
)

// DefaultRestartOnlyFields lists the configuration paths a running process
// cannot adopt: they decide connection pools, claim shards, goroutine counts and
// in-flight lease fencing. A change to any of them rejects the whole document so
// an operator has to roll out explicitly.
//
// The slice is read-only; callers that need extra paths use
// WatcherOptions.ExtraRestartOnlyFields.
var DefaultRestartOnlyFields = []string{
	"store",
	"worker.id",
	"worker.kinds",
	"worker.targets",
	"worker.concurrency",
	"worker.poll_interval",
	"worker.lease",
	"worker.heartbeat",
	"worker.hard_timeout",
	"worker.max_attempts",
	"publish.targets",
	"publish.tiup_binary",
}

// ReloadHandler is a callback invoked with the new config after a successful
// reload.
type ReloadHandler func(*Service)

// Snapshot is one immutable, validated configuration revision.
type Snapshot struct {
	// Generation is the revision number, starting at 1 for the initial load.
	Generation uint64
	// LoadedAt is when this revision was applied.
	LoadedAt time.Time
	// Service is the parsed configuration. Readers must treat it as read-only.
	Service *Service

	// hash identifies the raw file content that produced this revision.
	hash string
	// values is the flattened raw document, used to diff revisions and to detect
	// restart-only changes before normalization.
	values map[string]string
}

// WatcherOptions configures a Watcher.
type WatcherOptions struct {
	// PollInterval is how often Start re-reads the file. Zero means
	// DefaultPollInterval.
	PollInterval time.Duration
	// Normalize applies defaults and process-level overrides in place before
	// validation. It runs on every accepted revision, never on rejected ones.
	Normalize func(*Service)
	// Validate rejects a revision field by field. The initial load fails when it
	// returns an error; a reload keeps the previous snapshot.
	Validate func(*Service) error
	// ExtraRestartOnlyFields extends DefaultRestartOnlyFields with paths only
	// this process cannot adopt, for example tekton.reconciler_interval in the API.
	ExtraRestartOnlyFields []string
	// Logger receives the structured reload events. Nil disables logging.
	Logger *zerolog.Logger
}

// ReloadResult reports the outcome of one reload attempt.
type ReloadResult struct {
	// Generation is the applied revision after the attempt.
	Generation uint64
	// Candidate is the revision number the rejected document would have used.
	// It equals Generation for applied, unchanged and unreadable documents.
	Candidate uint64
	// Result is one of the Reload* constants.
	Result string
	// Changed lists the dotted paths whose values differ from the applied
	// revision. It is empty unless Result is ReloadSuccess.
	Changed []string
	// Fields lists the restart-only paths that changed. It is only set when
	// Result is ReloadRestartRequired.
	Fields []string
	// Err carries the field-level error for rejected or unreadable documents.
	Err error
}

// Watcher polls a YAML configuration file and atomically publishes validated
// revisions. Concurrent readers never observe a partially applied document.
type Watcher struct {
	path string
	opts WatcherOptions

	current atomic.Pointer[Snapshot]

	// mu serializes Reload so two polls cannot interleave.
	mu sync.Mutex

	handlersMu sync.RWMutex
	handlers   []ReloadHandler

	registry        *prometheus.Registry
	reloads         *prometheus.CounterVec
	generation      prometheus.Gauge
	lastReload      prometheus.Gauge
	restartRequired *prometheus.CounterVec
}

// NewWatcher loads the configuration once and returns a watcher for later
// revisions. It fails when the initial document cannot be read, parsed or
// validated: a process must not start on a configuration it rejected.
func NewWatcher(path string, opts WatcherOptions) (*Watcher, error) {
	if strings.TrimSpace(path) == "" {
		return nil, fmt.Errorf("config watcher: empty path")
	}
	if opts.PollInterval <= 0 {
		opts.PollInterval = DefaultPollInterval
	}
	if opts.Normalize == nil {
		opts.Normalize = func(*Service) {}
	}
	if opts.Validate == nil {
		opts.Validate = func(s *Service) error { return s.Validate() }
	}
	if opts.Logger == nil {
		nop := zerolog.Nop()
		opts.Logger = &nop
	}

	w := &Watcher{path: path, opts: opts}
	w.initMetrics()

	res, _ := w.reloadLocked(true)
	if res.Result != ReloadSuccess {
		return nil, fmt.Errorf("config watcher: initial load of %s: %w", path, res.Err)
	}
	return w, nil
}

func (w *Watcher) initMetrics() {
	w.registry = prometheus.NewRegistry()
	w.reloads = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "config_reload_total",
		Help: "Configuration reload attempts by outcome.",
	}, []string{"result"})
	w.generation = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "config_generation",
		Help: "Generation of the configuration revision currently applied.",
	})
	w.lastReload = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "config_last_reload_timestamp_seconds",
		Help: "Unix timestamp of the last successfully applied configuration revision.",
	})
	w.restartRequired = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "config_restart_required_total",
		Help: "Rejected configuration documents by the restart-only field that changed.",
	}, []string{"field"})
	w.registry.MustRegister(w.reloads, w.generation, w.lastReload, w.restartRequired)

	// Materialize every series so a scraper sees zeros before the first reload
	// and an absent series always means "the process is not watching config".
	for _, result := range []string{ReloadSuccess, ReloadUnchanged, ReloadInvalid, ReloadRestartRequired, ReloadReadError} {
		w.reloads.WithLabelValues(result).Add(0)
	}
	fields := append([]string(nil), DefaultRestartOnlyFields...)
	fields = append(fields, w.opts.ExtraRestartOnlyFields...)
	for _, field := range fields {
		w.restartRequired.WithLabelValues(field).Add(0)
	}
}

// Get returns the currently applied configuration. The returned value is
// shared and must not be mutated.
func (w *Watcher) Get() *Service {
	snapshot := w.current.Load()
	if snapshot == nil {
		return nil
	}
	return snapshot.Service
}

// Snapshot returns the currently applied revision.
func (w *Watcher) Snapshot() *Snapshot {
	return w.current.Load()
}

// Generation returns the generation of the applied configuration.
func (w *Watcher) Generation() uint64 {
	if snapshot := w.current.Load(); snapshot != nil {
		return snapshot.Generation
	}
	return 0
}

// OnReload registers a handler called after every applied revision. Handlers run
// in registration order and may call Get().
func (w *Watcher) OnReload(handler ReloadHandler) {
	if handler == nil {
		return
	}
	w.handlersMu.Lock()
	defer w.handlersMu.Unlock()
	w.handlers = append(w.handlers, handler)
}

// Start polls the file until ctx is cancelled.
func (w *Watcher) Start(ctx context.Context) {
	ticker := time.NewTicker(w.opts.PollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			w.Reload()
		}
	}
}

// Reload reads the file once and applies it when it is valid and contains no
// restart-only change. It is safe to call concurrently with Get and with SIGHUP
// handlers.
func (w *Watcher) Reload() ReloadResult {
	res, handlers := w.reloadLocked(false)
	for _, handler := range handlers {
		handler(w.Get())
	}
	return res
}

// MetricsGatherer exposes the watcher metrics for a /metrics endpoint.
func (w *Watcher) MetricsGatherer() prometheus.Gatherer {
	return w.registry
}

// reloadLocked performs one reload attempt and returns the handlers to notify.
// The caller must not hold w.mu. The initial load is not a reload: it reports
// the generation but does not count as a config_reload_total attempt.
func (w *Watcher) reloadLocked(initial bool) (ReloadResult, []ReloadHandler) {
	w.mu.Lock()
	defer w.mu.Unlock()

	previous := w.current.Load()
	applied := uint64(0)
	if previous != nil {
		applied = previous.Generation
	}
	res := ReloadResult{Generation: applied, Candidate: applied + 1}

	data, err := os.ReadFile(w.path)
	if err != nil {
		res.Result = ReloadReadError
		res.Err = fmt.Errorf("read %s: %w", w.path, err)
		w.observe(res, initial)
		return res, nil
	}

	hash := hashContent(data)
	if previous != nil && previous.hash == hash {
		res.Result = ReloadUnchanged
		res.Candidate = applied
		w.observe(res, initial)
		return res, nil
	}

	var parsed Service
	if err := yaml.Unmarshal(data, &parsed); err != nil {
		res.Result = ReloadInvalid
		res.Err = fmt.Errorf("parse %s: %w", w.path, err)
		w.observe(res, initial)
		return res, nil
	}
	values := flattenDocument(data)

	if previous != nil {
		if fields := w.restartOnlyChanges(previous.values, values); len(fields) > 0 {
			res.Result = ReloadRestartRequired
			res.Fields = fields
			res.Err = fmt.Errorf("%s requires a restart", strings.Join(fields, ", "))
			w.observe(res, initial)
			return res, nil
		}
	}

	w.opts.Normalize(&parsed)
	if err := w.opts.Validate(&parsed); err != nil {
		res.Result = ReloadInvalid
		res.Err = err
		w.observe(res, initial)
		return res, nil
	}

	if previous != nil {
		res.Changed = diffDocuments(previous.values, values)
	}
	snapshot := &Snapshot{
		Generation: res.Candidate,
		LoadedAt:   time.Now(),
		Service:    &parsed,
		hash:       hash,
		values:     values,
	}
	w.current.Store(snapshot)

	res.Result = ReloadSuccess
	res.Generation = snapshot.Generation
	w.observe(res, initial)
	return res, w.snapshotHandlers()
}

func (w *Watcher) snapshotHandlers() []ReloadHandler {
	w.handlersMu.RLock()
	defer w.handlersMu.RUnlock()
	handlers := make([]ReloadHandler, len(w.handlers))
	copy(handlers, w.handlers)
	return handlers
}

// observe records the metrics and the structured log line for one attempt.
// Field values are never logged, only their dotted paths.
func (w *Watcher) observe(res ReloadResult, initial bool) {
	if !initial {
		w.reloads.WithLabelValues(res.Result).Inc()
	}

	logger := w.opts.Logger
	if initial && res.Result != ReloadSuccess {
		// The constructor returns the error; a failed initial load is not a reload.
		return
	}
	switch res.Result {
	case ReloadSuccess:
		w.generation.Set(float64(res.Generation))
		w.lastReload.SetToCurrentTime()
		message := "config reload applied"
		if initial {
			message = "config loaded"
		}
		logger.Info().
			Uint64("generation", res.Generation).
			Str("changed", summarizePaths(res.Changed)).
			Msg(message)
	case ReloadUnchanged:
		logger.Debug().
			Uint64("generation", res.Generation).
			Msg("config reload unchanged")
	case ReloadInvalid:
		logger.Error().
			Uint64("generation", res.Generation).
			Uint64("candidate", res.Candidate).
			Err(res.Err).
			Msg("config reload rejected")
	case ReloadRestartRequired:
		for _, field := range res.Fields {
			w.restartRequired.WithLabelValues(field).Inc()
		}
		logger.Error().
			Uint64("generation", res.Generation).
			Uint64("candidate", res.Candidate).
			Str("reason", ReloadRestartRequired).
			Str("fields", summarizePaths(res.Fields)).
			Msg("config reload ignored")
	case ReloadReadError:
		logger.Warn().
			Uint64("generation", res.Generation).
			Err(res.Err).
			Msg("config reload deferred")
	}
}

// restartOnlyChanges returns the restart-only fields whose value changed.
func (w *Watcher) restartOnlyChanges(previous, next map[string]string) []string {
	fields := append([]string(nil), DefaultRestartOnlyFields...)
	fields = append(fields, w.opts.ExtraRestartOnlyFields...)

	changed := make([]string, 0, len(fields))
	for _, field := range fields {
		if len(changedUnder(previous, next, field)) > 0 {
			changed = append(changed, field)
		}
	}
	sort.Strings(changed)
	return changed
}

// changedUnder reports the concrete paths under field that differ between two
// flattened documents. A removed key is reported as a change.
func changedUnder(previous, next map[string]string, field string) []string {
	paths := make([]string, 0, 4)
	for path, value := range next {
		if !pathMatches(path, field) {
			continue
		}
		if old, ok := previous[path]; !ok || old != value {
			paths = append(paths, path)
		}
	}
	for path := range previous {
		if !pathMatches(path, field) {
			continue
		}
		if _, ok := next[path]; !ok {
			paths = append(paths, path)
		}
	}
	return paths
}

// pathMatches reports whether path is field or one of its children. It never
// matches a sibling with a shared prefix, so publish.targets does not match
// publish.tiup_mirrors.
func pathMatches(path, field string) bool {
	return path == field || strings.HasPrefix(path, field+".")
}

// diffDocuments lists the dotted paths whose values differ between two
// flattened documents.
func diffDocuments(previous, next map[string]string) []string {
	changed := make([]string, 0, len(next))
	for path, value := range next {
		if old, ok := previous[path]; !ok || old != value {
			changed = append(changed, path)
		}
	}
	for path := range previous {
		if _, ok := next[path]; !ok {
			changed = append(changed, path)
		}
	}
	sort.Strings(changed)
	return changed
}

// flattenDocument parses the raw YAML into a flat path -> scalar map. It is used
// for diffing only, so values are stringified for comparison.
func flattenDocument(data []byte) map[string]string {
	var document any
	if err := yaml.Unmarshal(data, &document); err != nil {
		return map[string]string{}
	}
	values := make(map[string]string)
	flattenValue("", document, values)
	return values
}

func flattenValue(prefix string, value any, out map[string]string) {
	switch typed := value.(type) {
	case map[string]any:
		for key, child := range typed {
			flattenValue(joinPath(prefix, key), child, out)
		}
	case map[any]any:
		for key, child := range typed {
			flattenValue(joinPath(prefix, fmt.Sprint(key)), child, out)
		}
	case []any:
		for index, child := range typed {
			flattenValue(fmt.Sprintf("%s.%d", prefix, index), child, out)
		}
	default:
		if prefix == "" {
			return
		}
		out[prefix] = fmt.Sprint(typed)
	}
}

func joinPath(prefix, key string) string {
	if prefix == "" {
		return key
	}
	return prefix + "." + key
}

// summarizePaths bounds a field list for logging.
func summarizePaths(paths []string) string {
	const limit = 20
	if len(paths) == 0 {
		return "[]"
	}
	if len(paths) <= limit {
		return "[" + strings.Join(paths, " ") + "]"
	}
	return fmt.Sprintf("[%s ...+%d]", strings.Join(paths[:limit], " "), len(paths)-limit)
}

func hashContent(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
