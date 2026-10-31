package config

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"github.com/rs/zerolog"
)

// baseConfig is the smallest document that passes every validation the watcher
// runs: nightly is enabled with a per-target window, and the worker shard is
// explicit so a restart-only change is detectable.
const baseConfig = `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 1h
`

func writeTempConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return path
}

func rewriteConfig(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("rewrite config: %v", err)
	}
}

func newTestWatcher(t *testing.T, path string, opts WatcherOptions) *Watcher {
	t.Helper()
	if opts.Logger == nil {
		logger := zerolog.Nop()
		opts.Logger = &logger
	}
	if opts.Validate == nil {
		opts.Validate = func(s *Service) error { return s.Validate() }
	}
	w, err := NewWatcher(path, opts)
	if err != nil {
		t.Fatalf("NewWatcher: %v", err)
	}
	return w
}

func TestNewWatcherLoadsInitialSnapshot(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	if got := w.Generation(); got != 1 {
		t.Fatalf("generation = %d, want 1", got)
	}
	svc := w.Get()
	if svc == nil {
		t.Fatal("Get() returned nil")
	}
	if svc.Store.Driver != "mysql" {
		t.Errorf("store.driver = %q, want mysql", svc.Store.Driver)
	}
	if got := svc.Publish.Nightly.WindowFor("tiup-staging").WindowDuration(); got != time.Hour {
		t.Errorf("window = %s, want 1h", got)
	}
	if got := testutil.ToFloat64(w.generation); got != 1 {
		t.Errorf("config_generation = %v, want 1", got)
	}
}

func TestNewWatcherFailsOnInvalidInitialConfig(t *testing.T) {
	path := writeTempConfig(t, `
publish:
  nightly:
    enabled: true
    targets:
      tiup-staging:
        window: "0s"
`)
	logger := zerolog.Nop()
	if _, err := NewWatcher(path, WatcherOptions{Logger: &logger}); err == nil {
		t.Fatal("NewWatcher accepted an invalid window")
	}
}

func TestWatcherReloadUnchanged(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	res := w.Reload()
	if res.Result != ReloadUnchanged {
		t.Fatalf("result = %q, want %q (err: %v)", res.Result, ReloadUnchanged, res.Err)
	}
	if res.Generation != 1 || w.Generation() != 1 {
		t.Fatalf("generation = %d/%d, want 1", res.Generation, w.Generation())
	}
	if got := testutil.ToFloat64(w.reloads.WithLabelValues(ReloadUnchanged)); got != 1 {
		t.Errorf("config_reload_total{unchanged} = %v, want 1", got)
	}
}

func TestWatcherReloadAppliesHotField(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 2h
`)
	res := w.Reload()
	if res.Result != ReloadSuccess {
		t.Fatalf("result = %q, want %q (err: %v)", res.Result, ReloadSuccess, res.Err)
	}
	if res.Generation != 2 || w.Generation() != 2 {
		t.Fatalf("generation = %d/%d, want 2", res.Generation, w.Generation())
	}
	if got := w.Get().Publish.Nightly.WindowFor("tiup-staging").WindowDuration(); got != 2*time.Hour {
		t.Errorf("window = %s, want 2h", got)
	}
	if !contains(res.Changed, "publish.nightly.targets.tiup-staging.window") {
		t.Errorf("changed = %v, want the nightly window path", res.Changed)
	}
	if got := testutil.ToFloat64(w.reloads.WithLabelValues(ReloadSuccess)); got != 1 {
		t.Errorf("config_reload_total{success} = %v, want 1", got)
	}
	if got := testutil.ToFloat64(w.generation); got != 2 {
		t.Errorf("config_generation = %v, want 2", got)
	}
}

func TestWatcherReloadRejectsInvalidConfig(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    targets:
      tiup-staging:
        window: "0s"
`)
	res := w.Reload()
	if res.Result != ReloadInvalid {
		t.Fatalf("result = %q, want %q", res.Result, ReloadInvalid)
	}
	if res.Err == nil || !strings.Contains(res.Err.Error(), "window") {
		t.Fatalf("err = %v, want a field-level window error", res.Err)
	}
	if w.Generation() != 1 {
		t.Fatalf("generation = %d, want the last-known-good 1", w.Generation())
	}
	if got := w.Get().Publish.Nightly.WindowFor("tiup-staging").WindowDuration(); got != time.Hour {
		t.Errorf("window = %s, want the last-known-good 1h", got)
	}
	if got := testutil.ToFloat64(w.reloads.WithLabelValues(ReloadInvalid)); got != 1 {
		t.Errorf("config_reload_total{invalid} = %v, want 1", got)
	}
}

func TestWatcherReloadRejectsRestartOnlyField(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["docker.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 1h
`)
	res := w.Reload()
	if res.Result != ReloadRestartRequired {
		t.Fatalf("result = %q, want %q", res.Result, ReloadRestartRequired)
	}
	if !contains(res.Fields, "worker.targets") {
		t.Fatalf("fields = %v, want worker.targets", res.Fields)
	}
	if got := w.Get().Worker.Targets; len(got) != 1 || got[0] != "gcr.io" {
		t.Errorf("worker.targets = %v, want the last-known-good [gcr.io]", got)
	}
	if got := testutil.ToFloat64(w.restartRequired.WithLabelValues("worker.targets")); got != 1 {
		t.Errorf("config_restart_required_total{worker.targets} = %v, want 1", got)
	}
}

func TestWatcherReloadKeepsSnapshotOnReadError(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	if err := os.Remove(path); err != nil {
		t.Fatalf("remove config: %v", err)
	}
	res := w.Reload()
	if res.Result != ReloadReadError {
		t.Fatalf("result = %q, want %q", res.Result, ReloadReadError)
	}
	if w.Generation() != 1 || w.Get() == nil {
		t.Fatalf("generation = %d, want the last-known-good 1", w.Generation())
	}
	if got := testutil.ToFloat64(w.reloads.WithLabelValues(ReloadReadError)); got != 1 {
		t.Errorf("config_reload_total{read_error} = %v, want 1", got)
	}
}

// TestWatcherFollowsSymlinkSwap reproduces the Kubernetes Secret layout: the
// pod mounts config.yaml as a symlink into ..data, and a Secret update swaps
// ..data atomically. The watcher must read the complete new document.
func TestWatcherFollowsSymlinkSwap(t *testing.T) {
	root := t.TempDir()
	first := filepath.Join(root, "..2026_10_31_00_00_00")
	second := filepath.Join(root, "..2026_10_31_01_00_00")
	for _, dir := range []string{first, second} {
		if err := os.Mkdir(dir, 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", dir, err)
		}
	}
	if err := os.WriteFile(filepath.Join(first, "config.yaml"), []byte(baseConfig), 0o600); err != nil {
		t.Fatalf("write first: %v", err)
	}
	if err := os.Symlink("..2026_10_31_00_00_00", filepath.Join(root, "..data")); err != nil {
		t.Fatalf("symlink ..data: %v", err)
	}
	path := filepath.Join(root, "config.yaml")
	if err := os.Symlink("..data/config.yaml", path); err != nil {
		t.Fatalf("symlink config.yaml: %v", err)
	}

	w := newTestWatcher(t, path, WatcherOptions{})

	updated := `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 3h
`
	if err := os.WriteFile(filepath.Join(second, "config.yaml"), []byte(updated), 0o600); err != nil {
		t.Fatalf("write second: %v", err)
	}
	tmp := filepath.Join(root, "..data_tmp")
	if err := os.Symlink("..2026_10_31_01_00_00", tmp); err != nil {
		t.Fatalf("symlink ..data_tmp: %v", err)
	}
	if err := os.Rename(tmp, filepath.Join(root, "..data")); err != nil {
		t.Fatalf("swap ..data: %v", err)
	}

	res := w.Reload()
	if res.Result != ReloadSuccess {
		t.Fatalf("result = %q, want %q (err: %v)", res.Result, ReloadSuccess, res.Err)
	}
	if got := w.Get().Publish.Nightly.WindowFor("tiup-staging").WindowDuration(); got != 3*time.Hour {
		t.Errorf("window = %s, want 3h", got)
	}
}

func TestWatcherStartPollsUntilContextIsCancelled(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{PollInterval: 20 * time.Millisecond})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		w.Start(ctx)
	}()

	rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 4h
`)

	deadline := time.Now().Add(2 * time.Second)
	for w.Generation() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := w.Generation(); got != 2 {
		t.Fatalf("generation = %d, want 2 after a poll", got)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Start did not return after the context was cancelled")
	}
}

func TestWatcherOnReloadHandlersReceiveTheNewSnapshot(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	var seen atomic.Value
	w.OnReload(func(svc *Service) { seen.Store(svc) })

	rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: 5h
`)
	res := w.Reload()
	if res.Result != ReloadSuccess {
		t.Fatalf("result = %q, want %q (err: %v)", res.Result, ReloadSuccess, res.Err)
	}
	got, _ := seen.Load().(*Service)
	if got == nil {
		t.Fatal("OnReload handler was not called")
	}
	if want := 5 * time.Hour; got.Publish.Nightly.WindowFor("tiup-staging").WindowDuration() != want {
		t.Errorf("handler window = %s, want %s", got.Publish.Nightly.WindowFor("tiup-staging").WindowDuration(), want)
	}
}

func TestWatcherNormalizeRunsBeforeValidate(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	logger := zerolog.Nop()
	w := newTestWatcher(t, path, WatcherOptions{
		Logger: &logger,
		Normalize: func(s *Service) {
			s.Worker.ID = "pod-0"
		},
		Validate: func(s *Service) error {
			if s.Worker.ID != "pod-0" {
				t.Errorf("worker.id at validation = %q, want the normalized pod-0", s.Worker.ID)
			}
			return s.Validate()
		},
	})
	if got := w.Get().Worker.ID; got != "pod-0" {
		t.Fatalf("worker.id = %q, want pod-0", got)
	}
}

func TestWatcherConcurrentGetAndReload(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	var stop atomic.Bool
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for !stop.Load() {
				svc := w.Get()
				if svc == nil {
					t.Error("Get() returned nil")
					return
				}
				_ = svc.Publish.Nightly.WindowFor("tiup-staging").WindowDuration()
			}
		}()
	}

	for i := 0; i < 20; i++ {
		window := "1h"
		if i%2 == 0 {
			window = "2h"
		}
		rewriteConfig(t, path, `
store:
  driver: mysql
  dsn: root@tcp(127.0.0.1:4000)/tibuild
worker:
  targets: ["gcr.io"]
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  nightly:
    enabled: true
    tick_interval: 1m
    targets:
      tiup-staging:
        window: `+window+`
`)
		w.Reload()
	}

	stop.Store(true)
	wg.Wait()

	if got := w.Generation(); got < 2 || got > 21 {
		t.Errorf("generation = %d, want between 2 and 21", got)
	}
}

func TestWatcherAccessorsAndMetricsGatherer(t *testing.T) {
	var zero Watcher
	if zero.Get() != nil {
		t.Error("zero watcher Get() should be nil")
	}
	if zero.Generation() != 0 {
		t.Errorf("zero watcher generation = %d, want 0", zero.Generation())
	}
	if zero.Snapshot() != nil {
		t.Error("zero watcher Snapshot() should be nil")
	}

	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	snapshot := w.Snapshot()
	if snapshot == nil || snapshot.Generation != 1 || snapshot.Service == nil || snapshot.LoadedAt.IsZero() {
		t.Fatalf("snapshot = %+v, want an applied revision 1", snapshot)
	}

	families, err := w.MetricsGatherer().Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	names := make([]string, 0, len(families))
	for _, family := range families {
		names = append(names, family.GetName())
	}
	for _, want := range []string{"config_generation", "config_last_reload_timestamp_seconds", "config_reload_total", "config_restart_required_total"} {
		if !contains(names, want) {
			t.Errorf("metric families = %v, want %s", names, want)
		}
	}
}

func TestFlattenDocument(t *testing.T) {
	values := flattenDocument([]byte(`
a:
  b: 1
list:
  - x
  - y: 2
scalar: value
`))
	for path, want := range map[string]string{
		"a.b":      "1",
		"list.0":   "x",
		"list.1.y": "2",
		"scalar":   "value",
	} {
		if got := values[path]; got != want {
			t.Errorf("%s = %q, want %q", path, got, want)
		}
	}

	// yaml.v3 keeps non-string keys as map[any]any; the flattener must handle it.
	out := map[string]string{}
	flattenValue("", map[any]any{"k": []any{"v"}}, out)
	if got := out["k.0"]; got != "v" {
		t.Errorf("k.0 = %q, want v", got)
	}

	// A scalar root document contributes no paths.
	out = map[string]string{}
	flattenValue("", "root", out)
	if len(out) != 0 {
		t.Errorf("scalar root produced %v, want no paths", out)
	}

	if got := flattenDocument([]byte("\tnot: yaml")); len(got) != 0 {
		t.Errorf("invalid yaml produced %v, want an empty map", got)
	}
}

func TestPathMatchesDoesNotMatchPrefixSiblings(t *testing.T) {
	if pathMatches("publish.tiup_mirrors.staging", "publish.targets") {
		t.Error("publish.tiup_mirrors matched publish.targets")
	}
	if !pathMatches("publish.targets", "publish.targets") {
		t.Error("an exact field path must match")
	}
	if !pathMatches("worker.targets.0", "worker.targets") {
		t.Error("a child path must match its field")
	}
	if pathMatches("publish.nightly.targets.tiup-staging.window", "publish.targets") {
		t.Error("publish.nightly.targets matched publish.targets")
	}
}

func TestSummarizePaths(t *testing.T) {
	if got := summarizePaths(nil); got != "[]" {
		t.Errorf("summarizePaths(nil) = %q", got)
	}
	if got := summarizePaths([]string{"a", "b"}); got != "[a b]" {
		t.Errorf("summarizePaths = %q", got)
	}
	many := make([]string, 0, 25)
	for i := 0; i < 25; i++ {
		many = append(many, "f")
	}
	if got := summarizePaths(many); !strings.HasSuffix(got, "...+5]") {
		t.Errorf("summarizePaths(25) = %q, want a bounded list", got)
	}
}

func TestWatcherReloadIgnoresCommentOnlyEdits(t *testing.T) {
	path := writeTempConfig(t, baseConfig)
	w := newTestWatcher(t, path, WatcherOptions{})

	rewriteConfig(t, path, "# a comment\n"+baseConfig)
	res := w.Reload()
	if res.Result != ReloadSuccess {
		t.Fatalf("result = %q, want %q", res.Result, ReloadSuccess)
	}
	if len(res.Changed) != 0 {
		t.Errorf("changed = %v, want no field changes for a comment edit", res.Changed)
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
