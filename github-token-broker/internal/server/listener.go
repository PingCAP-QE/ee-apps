package server

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
)

// Serve runs HTTP and, when enabled, HTTPS with the same handler. It binds all
// listeners before serving and returns listener failures to the caller.
func Serve(ctx context.Context, cfg config.Server, handler http.Handler, logger *slog.Logger) error {
	newServer := func(address string) *http.Server {
		return &http.Server{
			Addr: address, Handler: handler,
			ReadTimeout: cfg.ReadTimeout, WriteTimeout: cfg.WriteTimeout,
			IdleTimeout: 60 * time.Second,
			ErrorLog:    slog.NewLogLogger(logger.Handler(), slog.LevelError),
		}
	}
	servers := []*http.Server{newServer(cfg.Address)}
	if cfg.TLS.Enabled {
		// Fail startup on missing, malformed or mismatched credentials rather than
		// silently running HTTP only. Do not cache: projected Secret directories
		// rotate symlinks, and each new handshake must see the current key pair.
		load := func() (*tls.Certificate, error) {
			cert, err := tls.LoadX509KeyPair(cfg.TLS.CertFile, cfg.TLS.KeyFile)
			if err != nil {
				return nil, fmt.Errorf("load server TLS key pair: %w", err)
			}
			return &cert, nil
		}
		if _, err := load(); err != nil {
			return err
		}
		https := newServer(cfg.TLS.Address)
		https.TLSConfig = &tls.Config{
			MinVersion: tls.VersionTLS12,
			GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
				return load()
			},
		}
		servers = append(servers, https)
	}

	listeners := make([]net.Listener, 0, len(servers))
	for _, srv := range servers {
		listener, err := net.Listen("tcp", srv.Addr)
		if err != nil {
			return fmt.Errorf("listen on %s: %w", srv.Addr, err)
		}
		defer listener.Close()
		defer srv.Close() // Also close active connections if graceful shutdown times out.
		listeners = append(listeners, listener)
	}

	results := make(chan error, len(servers))
	for i, srv := range servers {
		listener := listeners[i]
		go func() {
			scheme := "http"
			if srv.TLSConfig != nil {
				scheme = "https"
			}
			logger.Info("starting github-token-broker listener", "scheme", scheme, "address", listener.Addr().String())
			var err error
			if srv.TLSConfig != nil {
				err = srv.ServeTLS(listener, "", "")
			} else {
				err = srv.Serve(listener)
			}
			if errors.Is(err, http.ErrServerClosed) {
				err = nil
			} else if err != nil {
				err = fmt.Errorf("%s server failed: %w", scheme, err)
			}
			results <- err
		}()
	}

	var serveErr error
	select {
	case <-ctx.Done():
	case serveErr = <-results:
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	// Drain both listeners concurrently within one bounded shutdown budget.
	shutdownResults := make(chan error, len(servers))
	for _, srv := range servers {
		go func() {
			err := srv.Shutdown(shutdownCtx)
			if err != nil {
				err = fmt.Errorf("shutdown %s: %w", srv.Addr, err)
			}
			shutdownResults <- err
		}()
	}
	for range servers {
		serveErr = errors.Join(serveErr, <-shutdownResults)
	}
	return serveErr
}
