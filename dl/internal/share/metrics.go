package share

import (
	"fmt"
	"net/http"
	"sync/atomic"
)

// Metrics contains only aggregate counters; no capability, URL, customer or
// repository values become labels. Expose this handler only on the probe port.
type Metrics struct {
	Issued   atomic.Uint64
	Denied   atomic.Uint64
	Active   atomic.Int64
	Failures atomic.Uint64
	Bytes    atomic.Uint64
}

func (m *Metrics) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = fmt.Fprintf(w, "# TYPE dl_delivery_issued_total counter\ndl_delivery_issued_total %d\n# TYPE dl_delivery_denied_total counter\ndl_delivery_denied_total %d\n# TYPE dl_delivery_active_transfers gauge\ndl_delivery_active_transfers %d\n# TYPE dl_delivery_transfer_failures_total counter\ndl_delivery_transfer_failures_total %d\n# TYPE dl_delivery_bytes_total counter\ndl_delivery_bytes_total %d\n", m.Issued.Load(), m.Denied.Load(), m.Active.Load(), m.Failures.Load(), m.Bytes.Load())
}
