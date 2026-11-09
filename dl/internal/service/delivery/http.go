// Package delivery supplies isolated, token-safe HTTP surfaces. Do not wrap
// these handlers with request/body logging, tracing or analytics middleware.
package delivery

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"time"

	sharesvr "github.com/PingCAP-QE/ee-apps/dl/gen/http/shares/server"
	api "github.com/PingCAP-QE/ee-apps/dl/gen/shares"
	"github.com/PingCAP-QE/ee-apps/dl/internal/share"
	goahttp "goa.design/goa/v3/http"
	"golang.org/x/time/rate"
)

type Limits struct {
	Concurrent        int
	RequestsPerSecond float64
	Burst             int
	TransferTimeout   time.Duration
}

// Public has no fallback mux, legacy middleware, management routes or probes.
// Rejected paths are never redirected or normalized into an authorized path.
func Public(s *share.Service, limits Limits) http.Handler {
	return limited(limits, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		secureHeaders(w)
		if r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
			// Never drain an anonymous request body before a large response.
			r.Close = true
			w.Header().Set("Connection", "close")
			fail(w, http.StatusNotFound)
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			fail(w, http.StatusNotFound)
			return
		}
		if r.URL.RawQuery != "" || r.URL.ForceQuery || r.URL.RawPath != "" || !strings.HasPrefix(r.URL.Path, "/d/") {
			fail(w, http.StatusNotFound)
			return
		}
		token := strings.TrimPrefix(r.URL.Path, "/d/")
		raw, err := base64.RawURLEncoding.Strict().DecodeString(token)
		if len(token) != 43 || err != nil || len(raw) != 32 {
			s.Metrics.Denied.Add(1)
			fail(w, http.StatusNotFound)
			return
		}
		hash := sha256.Sum256([]byte(token))
		dbctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		g, err := s.Store.Authorize(dbctx, hash[:], r.Method == http.MethodGet)
		cancel()
		if errors.Is(err, share.ErrNotFound) {
			s.Metrics.Denied.Add(1)
			fail(w, http.StatusNotFound)
			return
		}
		if err != nil {
			fail(w, http.StatusServiceUnavailable)
			return
		}
		issuer := s.Config.Issuer(g.IssuedBy)
		if issuer == nil || !issuer.Allows(g.Repository, g.SourceTag) {
			s.Metrics.Denied.Add(1)
			s.Record(g.ID, "authorize", "denied", 0)
			fail(w, http.StatusNotFound)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), limits.TransferTimeout)
		defer cancel()
		var rc io.ReadCloser
		if r.Method == http.MethodGet {
			rc, err = s.Backend.Fetch(ctx, g.Artifact)
			if err != nil {
				s.Metrics.Failures.Add(1)
				s.Record(g.ID, "transfer", "upstream_failed", 0)
				fail(w, http.StatusBadGateway)
				return
			}
			defer rc.Close()
		}
		w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": g.Filename}))
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Length", strconv.FormatInt(g.BlobSize, 10))
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		s.Record(g.ID, "transfer", "started", 0)
		s.Metrics.Active.Add(1)
		defer s.Metrics.Active.Add(-1)
		// Write deadlines bound stalled clients without imposing a short total
		// response timeout. Reset after each bounded chunk.
		rcWriter := http.NewResponseController(w)
		n, err := io.CopyBuffer(deadlineWriter{w, rcWriter}, rc, make([]byte, 32*1024))
		outcome := "stream_finished"
		if err != nil || n != g.BlobSize {
			outcome = "stream_failed"
			s.Metrics.Failures.Add(1)
		}
		s.Metrics.Bytes.Add(uint64(n))
		s.Record(g.ID, "transfer", outcome, n)
		if outcome == "stream_failed" {
			panic(http.ErrAbortHandler)
		}
	}))
}

type deadlineWriter struct {
	w          http.ResponseWriter
	controller *http.ResponseController
}

func (d deadlineWriter) Write(p []byte) (int, error) {
	_ = d.controller.SetWriteDeadline(time.Now().Add(2 * time.Minute))
	return d.w.Write(p)
}

func Management(s *share.Service, limits Limits) http.Handler {
	mux := goahttp.NewMuxer()
	server := sharesvr.New(api.NewEndpoints(s), mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, func(_ context.Context, w http.ResponseWriter, _ error) { fail(w, http.StatusInternalServerError) }, nil)
	sharesvr.Mount(mux, server)
	return limited(limits, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		secureHeaders(w)
		if r.URL.RawQuery != "" || r.URL.ForceQuery || r.URL.RawPath != "" || (r.URL.Path != "/api/v1/shares" && !strings.HasPrefix(r.URL.Path, "/api/v1/shares/")) {
			fail(w, http.StatusNotFound)
			return
		}
		values := r.Header.Values("Authorization")
		if len(values) != 1 || len(values[0]) > 1024 || !strings.HasPrefix(values[0], "Bearer ") || strings.ContainsAny(strings.TrimPrefix(values[0], "Bearer "), " \t\r\n") {
			fail(w, http.StatusUnauthorized)
			return
		}
		// Authenticate before Goa decodes the body, including oversized requests.
		ctx, cancel := context.WithTimeout(r.Context(), time.Minute)
		defer cancel()
		ctx, err := s.APIKeyAuth(ctx, strings.TrimPrefix(values[0], "Bearer "), nil)
		if err != nil {
			fail(w, http.StatusUnauthorized)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 16*1024)
		mux.ServeHTTP(w, r.WithContext(ctx))
	}))
}

func secureHeaders(w http.ResponseWriter) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
}

func fail(w http.ResponseWriter, status int) { http.Error(w, "delivery unavailable", status) }

func limited(limits Limits, next http.Handler) http.Handler {
	semaphore := make(chan struct{}, limits.Concurrent)
	limiter := rate.NewLimiter(rate.Limit(limits.RequestsPerSecond), limits.Burst)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		secureHeaders(w)
		if !limiter.Allow() {
			fail(w, http.StatusTooManyRequests)
			return
		}
		select {
		case semaphore <- struct{}{}:
			defer func() { <-semaphore }()
		default:
			fail(w, http.StatusServiceUnavailable)
			return
		}
		next.ServeHTTP(w, r)
	})
}
