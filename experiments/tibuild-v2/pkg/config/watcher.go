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
// produce a reliable event on the mounted file, so the watcher compares content
// hashes instead of watching the file.
const DefaultPollInterval = 15 * time.Second

// Reload outcomes, reported by ReloadResult.Result and config_reload_total.
const (
	ReloadSuccess         = "success"
	ReloadUnchanged       = "unchanged"
	ReloadInvalid         = "invalid"
	ReloadRestartRequired = "restart_required"
	ReloadReadError       = "read_error"
)

// DefaultRestartOnlyFields lists the configuration paths a running process
// cannot adopt: they decide connection pools, claim shards, goroutine counts and
// lease fencing. A change to one of them rejects the whole document so an
// operator has to roll out instead of running a half-applied configuration.
//
// The slice is read-only; use WatcherOptions.ExtraRestartOnlyFields for paths
// only one binary owns.
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

// ReloadHandler is called with the applied configuration after a reload.
type ReloadHandler func(*Service)

// Snapshot is one immutable, validated configuration revision.
type Snapshot struct {
	// Generation is the revision number, starting at 1.
	Generation uint64
	// LoadedAt is when the revision was applied.
	LoadedAt time.Time
	// Service is the parsed configuration. Treat it as read-only.
	Service *Service

	hash   string
	values map[string]string
}

// WatcherOptions configures a Watcher.
type WatcherOptions struct {
	// PollInterval is how often Start re-reads the file. Zero means
	// DefaultPollInterval.
	PollInterval time.Duration
	// Normalize applies defaults and process-level overrides in place before
	// validation. It never runs on a rejected document.
	Normalize func(*Service)
	// Validate rejects a revision field by field. Nil uses Service.Validate.
	// The initial load fails on error; a reload keeps the previous snapshot.
	Validate func(*Service) error
	// ExtraRestartOnlyFields extends DefaultRestartOnlyFields.
	ExtraRestartOnlyFields []string
	// Logger receives the structured reload events. Nil disables logging.
	Logger *zerolog.Logger
	// Registerer receives the reload metrics. Nil disables them.
	Registerer prometheus.Registerer
}

// ReloadResult reports the outcome of one reload attempt.
type ReloadResult struct {
	// Generation is the applied revision after the attempt.
	Generation uint64
	// Candidate is the revision the rejected document would have used. It
	// equals Generation for applied, unchanged and unreadable documents.
	Candidate uint64
	// Result is one of the Reload* constants.
	Result string
	// Changed lists the dotted paths that differ from the applied revision.
	Changed []string
	// Fields lists the restart-only paths that changed.
	Fields []string
	// Err carries the field-level error for rejected or unreadable documents.
	Err error

	// repeat marks a rejection of the document that was already rejected by the
	// previous attempt, so the poll loop logs it once instead of every interval.
	repeat bool
}

// Watcher polls a YAML configuration file and publishes validated revisions
// atomically, so concurrent readers never observe a partially applied document.
type Watcher struct {
	path    string
	opts    WatcherOptions
	metrics *watcherMetrics

	current atomic.Pointer[Snapshot]

	mu         sync.Mutex // serializes reloads
	handlersMu sync.RWMutex
	handlers   []ReloadHandler

	// lastRejectKey identifies the document (content hash or read error) that
	// the previous attempt rejected, so a rejected file is logged once instead
	// of every poll interval. Guarded by mu.
	lastRejectKey    string
	lastRejectResult string
}

// watcherMetrics is nil when no registerer was supplied.
type watcherMetrics struct {
	reloads         *prometheus.CounterVec
	generation      prometheus.Gauge
	lastReload      prometheus.Gauge
	restartRequired *prometheus.CounterVec
}

func newWatcherMetrics(registerer prometheus.Registerer, extraFields []string) *watcherMetrics {
	if registerer == nil {
		return nil
	}
	m := &watcherMetrics{
		reloads: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "config_reload_total",
			Help: "Configuration reload attempts by outcome.",
		}, []string{"result"}),
		generation: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "config_generation",
			Help: "Generation of the configuration revision currently applied.",
		}),
		lastReload: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "config_last_reload_timestamp_seconds",
			Help: "Unix timestamp of the last successfully applied revision.",
		}),
		restartRequired: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "config_restart_required_total",
			Help: "Rejected documents by the restart-only field that changed.",
		}, []string{"field"}),
	}
	registerer.MustRegister(m.reloads, m.generation, m.lastReload, m.restartRequired)
	// Materialize every series so an absent one means "no watcher", not
	// "no reloads yet".
	for _, result := range []string{ReloadSuccess, ReloadUnchanged, ReloadInvalid, ReloadRestartRequired, ReloadReadError} {
		m.reloads.WithLabelValues(result).Add(0)
	}
	fields := append(append([]string(nil), DefaultRestartOnlyFields...), extraFields...)
	for _, field := range fields {
		m.restartRequired.WithLabelValues(field).Add(0)
	}
	return m
}

func (m *watcherMetrics) record(res ReloadResult, initial bool) {
	if m == nil || initial {
		return
	}
	m.reloads.WithLabelValues(res.Result).Inc()
	if res.Result == ReloadRestartRequired {
		for _, field := range res.Fields {
			m.restartRequired.WithLabelValues(field).Inc()
		}
	}
}

func (m *watcherMetrics) recordApplied(res ReloadResult) {
	if m == nil {
		return
	}
	m.generation.Set(float64(res.Generation))
	m.lastReload.SetToCurrentTime()
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

	w := &Watcher{
		path:    path,
		opts:    opts,
		metrics: newWatcherMetrics(opts.Registerer, opts.ExtraRestartOnlyFields),
	}
	if res, _ := w.reloadLocked(true); res.Result != ReloadSuccess {
		return nil, fmt.Errorf("config watcher: initial load of %s: %w", path, res.Err)
	}
	return w, nil
}

// Get returns the applied configuration. The value is shared: do not mutate it.
func (w *Watcher) Get() *Service {
	if snapshot := w.current.Load(); snapshot != nil {
		return snapshot.Service
	}
	return nil
}

// Snapshot returns the applied revision.
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
// in registration order and may call Get.
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
// restart-only change. It is also the SIGHUP path and is safe to call
// concurrently with Get.
func (w *Watcher) Reload() ReloadResult {
	res, handlers := w.reloadLocked(false)
	for _, handler := range handlers {
		handler(w.Get())
	}
	return res
}

// reloadLocked performs one reload attempt and returns the handlers to notify.
// The initial load reports its generation but is not a reload attempt.
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
		res.repeat = w.noteRejection(res.Result, err.Error())
		w.observe(res, initial)
		return res, nil
	}

	hash := hashContent(data)
	if previous != nil && previous.hash == hash {
		res.Result = ReloadUnchanged
		res.Candidate = applied
		w.clearRejection()
		w.observe(res, initial)
		return res, nil
	}

	var parsed Service
	if err := yaml.Unmarshal(data, &parsed); err != nil {
		res.Result = ReloadInvalid
		res.Err = fmt.Errorf("parse %s: %w", w.path, err)
		res.repeat = w.noteRejection(res.Result, hash)
		w.observe(res, initial)
		return res, nil
	}
	values := flattenDocument(data)

	if previous != nil {
		if fields := w.restartOnlyChanges(previous.values, values); len(fields) > 0 {
			res.Result = ReloadRestartRequired
			res.Fields = fields
			res.Err = fmt.Errorf("%s requires a restart", strings.Join(fields, ", "))
			res.repeat = w.noteRejection(res.Result, hash)
			w.observe(res, initial)
			return res, nil
		}
	}

	w.opts.Normalize(&parsed)
	if err := w.opts.Validate(&parsed); err != nil {
		res.Result = ReloadInvalid
		res.Err = err
		res.repeat = w.noteRejection(res.Result, hash)
		w.observe(res, initial)
		return res, nil
	}

	if previous != nil {
		res.Changed = diffDocuments(previous.values, values)
	}
	w.current.Store(&Snapshot{
		Generation: res.Candidate,
		LoadedAt:   time.Now(),
		Service:    &parsed,
		hash:       hash,
		values:     values,
	})

	res.Result = ReloadSuccess
	res.Generation = res.Candidate
	w.clearRejection()
	w.observe(res, initial)
	return res, w.snapshotHandlers()
}

func (w *Watcher) snapshotHandlers() []ReloadHandler {
	w.handlersMu.RLock()
	defer w.handlersMu.RUnlock()
	return append([]ReloadHandler(nil), w.handlers...)
}

// noteRejection records which document the attempt rejected and reports whether
// the previous attempt rejected the same one.
func (w *Watcher) noteRejection(result, key string) bool {
	repeat := w.lastRejectResult == result && w.lastRejectKey == key
	w.lastRejectResult, w.lastRejectKey = result, key
	return repeat
}

// clearRejection forgets the last rejected document after an accepted one.
func (w *Watcher) clearRejection() {
	w.lastRejectResult, w.lastRejectKey = "", ""
}

// observe records the metrics and the structured log line for one attempt. Only
// field paths are logged, never field values. A document that stays rejected is
// logged once at error level and then at debug level: the mount retries every
// poll interval, and an operator should not get four errors a minute per pod
// until they fix the file.
func (w *Watcher) observe(res ReloadResult, initial bool) {
	if initial && res.Result != ReloadSuccess {
		return // the constructor returns the error; this is not a reload
	}
	w.metrics.record(res, initial)
	if res.Result == ReloadSuccess {
		w.metrics.recordApplied(res)
	}

	logger := w.opts.Logger
	if res.repeat {
		logger.Debug().
			Uint64("generation", res.Generation).
			Uint64("candidate", res.Candidate).
			Str("result", res.Result).
			Err(res.Err).
			Msg("config reload still rejected, document unchanged")
		return
	}
	switch res.Result {
	case ReloadSuccess:
		message := "config reload applied"
		if initial {
			message = "config loaded"
		}
		logger.Info().
			Uint64("generation", res.Generation).
			Str("changed", summarizePaths(res.Changed)).
			Msg(message)
	case ReloadUnchanged:
		logger.Debug().Uint64("generation", res.Generation).Msg("config reload unchanged")
	case ReloadInvalid:
		logger.Error().
			Uint64("generation", res.Generation).
			Uint64("candidate", res.Candidate).
			Err(res.Err).
			Msg("config reload rejected")
	case ReloadRestartRequired:
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
	fields := append(append([]string(nil), DefaultRestartOnlyFields...), w.opts.ExtraRestartOnlyFields...)

	changed := make([]string, 0, len(fields))
	for _, field := range fields {
		if len(changedUnder(previous, next, field)) > 0 {
			changed = append(changed, field)
		}
	}
	sort.Strings(changed)
	return changed
}

// changedUnder reports the paths under field that differ between two flattened
// documents. A removed key counts as a change.
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

// pathMatches reports whether path is field or one of its children, never a
// sibling with a shared prefix: publish.targets does not match publish.tiup_mirrors.
func pathMatches(path, field string) bool {
	return path == field || strings.HasPrefix(path, field+".")
}

// diffDocuments lists the paths whose values differ between two documents.
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

// flattenDocument maps every leaf of the raw YAML to its dotted path. It is used
// for diffing, so values are stringified for comparison only.
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
		if prefix != "" {
			out[prefix] = fmt.Sprint(typed)
		}
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
	switch {
	case len(paths) == 0:
		return "[]"
	case len(paths) <= limit:
		return "[" + strings.Join(paths, " ") + "]"
	default:
		return fmt.Sprintf("[%s ...+%d]", strings.Join(paths[:limit], " "), len(paths)-limit)
	}
}

func hashContent(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
