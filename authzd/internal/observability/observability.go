package observability

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"sync/atomic"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/decision"
)

type Recorder struct {
	logger    *slog.Logger
	allowed   atomic.Uint64
	denied    atomic.Uint64
	errors    atomic.Uint64
	wouldDeny atomic.Uint64
}

func NewRecorder(logger *slog.Logger) *Recorder {
	if logger == nil {
		logger = slog.Default()
	}
	return &Recorder{logger: logger}
}

func (r *Recorder) Record(ctx context.Context, event decision.AuditEvent) {
	switch event.Outcome {
	case "allow":
		r.allowed.Add(1)
	case "deny":
		r.denied.Add(1)
	default:
		r.errors.Add(1)
	}
	if event.WouldDeny {
		r.wouldDeny.Add(1)
	}
	r.logger.InfoContext(ctx, "authorization decision",
		"decision_id", event.DecisionID,
		"principal", event.Principal,
		"capability", event.Capability,
		"resource_type", event.ResourceType,
		"resource_id", event.ResourceID,
		"outcome", event.Outcome,
		"would_deny", event.WouldDeny,
		"reason_code", event.Reason,
		"model_id", event.ModelID,
		"registry_version", event.RegistryVersion,
		"resolver_version", event.ResolverVersion,
		"correlation_id", event.CorrelationID,
	)
}

func (r *Recorder) Handler(ready func() bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if ready != nil && !ready() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready\n"))
	})
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		_, _ = fmt.Fprintf(w,
			"# TYPE authzd_decisions_total counter\nauthzd_decisions_total{outcome=\"allow\"} %d\nauthzd_decisions_total{outcome=\"deny\"} %d\nauthzd_decisions_total{outcome=\"error\"} %d\n# TYPE authzd_would_deny_total counter\nauthzd_would_deny_total %d\n",
			r.allowed.Load(), r.denied.Load(), r.errors.Load(), r.wouldDeny.Load(),
		)
	})
	return mux
}
