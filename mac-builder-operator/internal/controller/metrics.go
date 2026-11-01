package controller

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
	macAgentReady = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "macagent_ready",
		Help: "1 when the MacAgent is ready, 0 otherwise.",
	}, []string{"agent"})

	macBuildAssignments = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "macbuild_assignment_total",
		Help: "MacBuild scheduling outcomes.",
	}, []string{"result"})

	macBuildAgentLost = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "macbuild_agent_lost_total",
		Help: "Builds failed because their MacAgent stopped reporting.",
	})
)

func init() {
	metrics.Registry.MustRegister(macAgentReady, macBuildAssignments, macBuildAgentLost)
}

func boolToFloat(value bool) float64 {
	if value {
		return 1
	}
	return 0
}
