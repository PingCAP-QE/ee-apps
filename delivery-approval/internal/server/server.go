// Package server wires the HTTP surfaces. The application surface is the
// OIDC-protected approval UI; the probe listener is independent and never
// exposed through the Gateway.
package server

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/auth"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/web"
)

type Options struct {
	Store   store.Store
	Config  *config.Manager
	Auth    auth.Validator
	Metrics *metrics.Metrics
	Logger  *log.Logger
}

// Application is the OIDC-protected form/detail surface.
func Application(opts Options) http.Handler {
	return web.New(web.Options{
		Store:   opts.Store,
		Config:  opts.Config,
		Auth:    opts.Auth,
		Metrics: opts.Metrics,
		Logger:  opts.Logger,
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
