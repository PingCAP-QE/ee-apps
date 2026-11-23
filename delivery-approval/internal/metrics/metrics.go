// Package metrics exposes aggregate counters on the private probe listener.
package metrics

import (
	"fmt"
	"net/http"
	"sync/atomic"
)

// Metrics intentionally has no request, customer, artifact or URL labels.
type Metrics struct {
	Created        atomic.Uint64
	Approved       atomic.Uint64
	Rejected       atomic.Uint64
	Denied         atomic.Uint64
	DeliveryFailed atomic.Uint64
	ActiveRequests atomic.Int64
}

func (m *Metrics) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = fmt.Fprintf(w,
		"# TYPE dl_approval_requests_created_total counter\n"+
			"dl_approval_requests_created_total %d\n"+
			"# TYPE dl_approval_requests_approved_total counter\n"+
			"dl_approval_requests_approved_total %d\n"+
			"# TYPE dl_approval_requests_rejected_total counter\n"+
			"dl_approval_requests_rejected_total %d\n"+
			"# TYPE dl_approval_requests_denied_total counter\n"+
			"dl_approval_requests_denied_total %d\n"+
			"# TYPE dl_approval_delivery_failed_total counter\n"+
			"dl_approval_delivery_failed_total %d\n"+
			"# TYPE dl_approval_active_requests gauge\n"+
			"dl_approval_active_requests %d\n",
		m.Created.Load(), m.Approved.Load(), m.Rejected.Load(), m.Denied.Load(),
		m.DeliveryFailed.Load(), m.ActiveRequests.Load())
}
