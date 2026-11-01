package workspace

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var cleanupFailures = prometheus.NewCounter(prometheus.CounterOpts{
	Name: "macbuilder_workspace_cleanup_failures_total",
	Help: "Build workspace directories that could not be removed.",
})

func init() {
	metrics.Registry.MustRegister(cleanupFailures)
}
