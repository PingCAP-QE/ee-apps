// Package server wires the HTTP surfaces. The application surface is a
// placeholder in this phase; the probe listener is independent and never
// exposed through the Gateway.
package server

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type Options struct {
	Store   store.Store
	Metrics *metrics.Metrics
	Logger  *log.Logger
}

// Application is the future OIDC-protected form/API surface. Until the form
// lands, everything is a 404 so there is no route with accidental semantics.
func Application(_ Options) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	})
}

// Probes serves liveness, readiness and metrics on the private listener.
func Probes(opts Options) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /livez", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if opts.Store == nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		if err := opts.Store.Ready(ctx); err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	if opts.Metrics != nil {
		mux.Handle("GET /metrics", opts.Metrics)
	}
	return mux
}
