package main

import (
	"context"
	"net/http"
	"net/url"
	"sync"
	"time"

	"goa.design/clue/debug"
	"goa.design/clue/health"
	"goa.design/clue/log"
	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/fileserver"
	fileserversvr "github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/http/fileserver/server"
	imagesvr "github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/http/image/server"
	tidbcloudsvr "github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/http/tidbcloud/server"
	tiupsvr "github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/http/tiup/server"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tiup"
)

// handleHTTPServer starts configures and starts a HTTP server on the given
// URL. It shuts down the server if any error is received in the error channel.
func handleHTTPServer(ctx context.Context, u *url.URL,
	tiupEndpoints *tiup.Endpoints,
	fileserverEndpoints *fileserver.Endpoints,
	imageEndpoints *image.Endpoints,
	tidbcloudEndpoints *tidbcloud.Endpoints,
	wg *sync.WaitGroup, errc chan error, dbg bool) {

	// Provide the transport specific request decoder and response encoder.
	// The goa http package has built-in support for JSON, XML and gob.
	// Other encodings can be used by providing the corresponding functions,
	// see goa.design/implement/encoding.
	var (
		dec = goahttp.RequestDecoder
		enc = goahttp.ResponseEncoder
	)

	// Build the service HTTP request multiplexer and mount debug and profiler
	// endpoints in debug mode.
	var mux goahttp.Muxer
	{
		mux = goahttp.NewMuxer()
		if dbg {
			// Mount pprof handlers for memory profiling under /debug/pprof.
			debug.MountPprofHandlers(debug.Adapt(mux))
			// Mount /debug endpoint to enable or disable debug logs at runtime.
			debug.MountDebugLogEnabler(debug.Adapt(mux))
		}
	}

	// Wrap the endpoints with the transport specific layers. The generated
	// server packages contains code generated from the design which maps
	// the service input and output data structures to HTTP requests and
	// responses.
	var (
		tiupServer       *tiupsvr.Server
		fileserverServer *fileserversvr.Server
		imageServer      *imagesvr.Server
		tidbcloudServer  *tidbcloudsvr.Server
	)
	{
		eh := errorHandler(ctx)
		tiupServer = tiupsvr.New(tiupEndpoints, mux, dec, enc, eh, nil)
		fileserverServer = fileserversvr.New(fileserverEndpoints, mux, dec, enc, eh, nil)
		imageServer = imagesvr.New(imageEndpoints, mux, dec, enc, eh, nil)
		tidbcloudServer = tidbcloudsvr.New(tidbcloudEndpoints, mux, dec, enc, eh, nil)
	}

	// Configure the mux.
	tiupsvr.Mount(mux, tiupServer)
	fileserversvr.Mount(mux, fileserverServer)
	imagesvr.Mount(mux, imageServer)
	tidbcloudsvr.Mount(mux, tidbcloudServer)

	// ** Mount health check handler **
	check := health.Handler(health.NewChecker())
	mux.Handle("GET", "/healthz", check)
	mux.Handle("GET", "/livez", check)

	var handler http.Handler = mux
	if dbg {
		// Log query and response bodies if debug logs are enabled.
		handler = debug.HTTP()(handler)
	}
	handler = log.HTTP(ctx)(handler)

	// Start HTTP server using default configuration, change the code to
	// configure the server as required by your service.
	srv := &http.Server{Addr: u.Host, Handler: handler, ReadHeaderTimeout: time.Second * 60}
	for _, m := range tiupServer.Mounts {
		log.Printf(ctx, "HTTP %q mounted on %s %s", m.Method, m.Verb, m.Pattern)
	}
	for _, m := range fileserverServer.Mounts {
		log.Printf(ctx, "HTTP %q mounted on %s %s", m.Method, m.Verb, m.Pattern)
	}
	for _, m := range imageServer.Mounts {
		log.Printf(ctx, "HTTP %q mounted on %s %s", m.Method, m.Verb, m.Pattern)
	}

	wg.Go(func() {
		// Start HTTP server in a separate goroutine.
		go func() {
			log.Printf(ctx, "HTTP server listening on %q", u.Host)
			errc <- srv.ListenAndServe()
		}()

		<-ctx.Done()
		log.Printf(ctx, "shutting down HTTP server at %q", u.Host)

		// Shutdown gracefully with a 30s timeout.
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		err := srv.Shutdown(ctx)
		if err != nil {
			log.Printf(ctx, "failed to shutdown: %v", err)
		}
	})
}

// errorHandler returns a function that writes and logs the given error.
// The function also writes and logs the error unique ID so that it's possible
// to correlate.
func errorHandler(logCtx context.Context) func(context.Context, http.ResponseWriter, error) {
	return func(ctx context.Context, w http.ResponseWriter, err error) {
		log.Printf(logCtx, "ERROR: %s", err.Error())
	}
}
