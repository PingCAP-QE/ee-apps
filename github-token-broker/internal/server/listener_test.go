package server

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
)

func testKeyPair(t *testing.T, dir string, serial int64) *x509.Certificate {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: "localhost"},
		DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IsCA:        true, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, public, private)
	if err != nil {
		t.Fatal(err)
	}
	key, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	for name, block := range map[string]*pem.Block{
		"tls.crt": {Type: "CERTIFICATE", Bytes: der},
		"tls.key": {Type: "PRIVATE KEY", Bytes: key},
	} {
		if err := os.WriteFile(filepath.Join(dir, name), pem.EncodeToMemory(block), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func freeAddress(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	return address
}

func listenerConfig(t *testing.T) config.Server {
	t.Helper()
	return config.Server{
		Address: freeAddress(t), ReadTimeout: time.Second, WriteTimeout: time.Second,
		ShutdownTimeout: time.Second,
	}
}

func runListener(t *testing.T, cfg config.Server, logger *slog.Logger) func() error {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- Serve(ctx, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, r.URL.Path)
		}), logger)
	}()
	t.Cleanup(cancel)
	return func() error {
		cancel()
		select {
		case err := <-done:
			return err
		case <-time.After(3 * time.Second):
			t.Fatal("listener did not stop")
			return nil
		}
	}
}

func waitHTTP(t *testing.T, address string) {
	t.Helper()
	client := &http.Client{Timeout: 100 * time.Millisecond}
	defer client.CloseIdleConnections()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		response, err := client.Get("http://" + address + "/healthz")
		if err == nil {
			response.Body.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("HTTP listener did not start")
}

func TestServeHTTPCompatibility(t *testing.T) {
	cfg := listenerConfig(t)
	cfg.TLS.CertFile = "/not/present" // Disabled TLS must not touch these files.
	stop := runListener(t, cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	waitHTTP(t, cfg.Address)
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

func TestServeTLSAndProjectedSecretRotation(t *testing.T) {
	dir := t.TempDir()
	for _, version := range []string{"one", "two"} {
		if err := os.Mkdir(filepath.Join(dir, version), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	first := testKeyPair(t, filepath.Join(dir, "one"), 1)
	second := testKeyPair(t, filepath.Join(dir, "two"), 2)
	// Model Kubernetes' whole-directory Secret projection, not an in-place edit.
	if err := os.Symlink("one", filepath.Join(dir, "..data")); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"tls.crt", "tls.key"} {
		if err := os.Symlink("..data/"+name, filepath.Join(dir, name)); err != nil {
			t.Fatal(err)
		}
	}
	cfg := listenerConfig(t)
	cfg.TLS = config.TLS{Enabled: true, Address: freeAddress(t), CertFile: filepath.Join(dir, "tls.crt"), KeyFile: filepath.Join(dir, "tls.key")}
	logs, err := os.CreateTemp(t.TempDir(), "server-*.log")
	if err != nil {
		t.Fatal(err)
	}
	defer logs.Close()
	stop := runListener(t, cfg, slog.New(slog.NewJSONHandler(logs, &slog.HandlerOptions{Level: slog.LevelError})))
	waitHTTP(t, cfg.Address)
	roots := x509.NewCertPool()
	roots.AddCert(first)
	roots.AddCert(second)
	transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots}, DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: time.Second}
	check := func(serial int64) {
		t.Helper()
		for _, path := range []string{"/v1/token", "/healthz", "/readyz", "/metrics"} {
			response, err := client.Get("https://" + cfg.TLS.Address + path)
			if err != nil {
				t.Fatal(err)
			}
			body, err := io.ReadAll(response.Body)
			response.Body.Close()
			if err != nil || string(body) != path || response.TLS.PeerCertificates[0].SerialNumber.Int64() != serial {
				t.Fatalf("wrong handler/certificate: body=%q, TLS=%#v, err=%v", body, response.TLS, err)
			}
		}
	}
	check(1)

	// Unknown roots and TLS < 1.2 must be rejected; no insecure client fallback.
	untrusted := &http.Client{Timeout: time.Second}
	if response, err := untrusted.Get("https://" + cfg.TLS.Address + "/healthz"); err == nil {
		response.Body.Close()
		t.Fatal("untrusted certificate accepted")
	}
	connection, err := tls.DialWithDialer(&net.Dialer{Timeout: time.Second}, "tcp", cfg.TLS.Address, &tls.Config{
		RootCAs: roots, ServerName: "localhost", MinVersion: tls.VersionTLS10, MaxVersion: tls.VersionTLS11,
	})
	if err == nil {
		connection.Close()
		t.Fatal("TLS 1.1 accepted")
	}

	if err := os.Symlink("two", filepath.Join(dir, "..next")); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(dir, "..next"), filepath.Join(dir, "..data")); err != nil {
		t.Fatal(err)
	}
	check(2)
	// An invalid rotation fails closed; restoring the files recovers without restart.
	if err := os.WriteFile(filepath.Join(dir, "two", "tls.key"), []byte("invalid"), 0o600); err != nil {
		t.Fatal(err)
	}
	if response, err := client.Get("https://" + cfg.TLS.Address + "/healthz"); err == nil {
		response.Body.Close()
		t.Fatal("invalid rotated key accepted")
	}
	roots.AddCert(testKeyPair(t, filepath.Join(dir, "two"), 3))
	check(3)
	waitHTTP(t, cfg.Address) // Existing HTTP callers still work alongside TLS.
	if err := stop(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(logs.Name())
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var entry struct {
			Level   string `json:"level"`
			Message string `json:"msg"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatalf("invalid JSON log: %v", err)
		}
		if entry.Level != "ERROR" {
			t.Fatalf("expected error-level log, got %s", line)
		}
		if strings.Contains(entry.Message, "http: TLS handshake error") && strings.Contains(entry.Message, "load server TLS key pair") {
			found = true
		}
	}
	if !found {
		t.Fatal("invalid rotated key was not logged through the configured JSON logger")
	}
	for _, address := range []string{cfg.Address, cfg.TLS.Address} {
		listener, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatalf("listener not released after shutdown: %v", err)
		}
		listener.Close()
	}
}

func TestServeStartupFailures(t *testing.T) {
	for _, failure := range []string{"missing cert", "missing key", "malformed cert", "malformed key", "mismatched key", "HTTP bind", "TLS bind"} {
		t.Run(failure, func(t *testing.T) {
			dir := t.TempDir()
			testKeyPair(t, dir, 1)
			cfg := listenerConfig(t)
			cfg.TLS = config.TLS{Enabled: true, Address: freeAddress(t), CertFile: filepath.Join(dir, "tls.crt"), KeyFile: filepath.Join(dir, "tls.key")}
			switch failure {
			case "missing cert":
				cfg.TLS.CertFile += ".missing"
			case "missing key":
				cfg.TLS.KeyFile += ".missing"
			case "malformed cert", "malformed key":
				path := cfg.TLS.CertFile
				if strings.HasSuffix(failure, "key") {
					path = cfg.TLS.KeyFile
				}
				if err := os.WriteFile(path, []byte("not PEM"), 0o600); err != nil {
					t.Fatal(err)
				}
			case "mismatched key":
				other := t.TempDir()
				testKeyPair(t, other, 2)
				cfg.TLS.KeyFile = filepath.Join(other, "tls.key")
			case "HTTP bind", "TLS bind":
				listener, err := net.Listen("tcp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				if failure == "HTTP bind" {
					cfg.Address = listener.Addr().String()
				} else {
					cfg.TLS.Address = listener.Addr().String()
				}
			}
			if err := Serve(context.Background(), cfg, http.NotFoundHandler(), slog.Default()); err == nil {
				t.Fatal("expected startup failure")
			}
			if failure != "HTTP bind" {
				listener, err := net.Listen("tcp", cfg.Address)
				if err != nil {
					t.Fatalf("HTTP listener leaked on startup failure: %v", err)
				}
				listener.Close()
			}
		})
	}
}

func TestServeShutdownTimeout(t *testing.T) {
	cfg := listenerConfig(t)
	cfg.ShutdownTimeout = time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started, release := make(chan struct{}), make(chan struct{})
	defer close(release)
	done := make(chan error, 1)
	go func() {
		done <- Serve(ctx, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			close(started)
			<-release
		}), slog.Default())
	}()
	requestDone := make(chan struct{})
	go func() {
		defer close(requestDone)
		client := &http.Client{Timeout: time.Second}
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			response, err := client.Get("http://" + cfg.Address)
			if err == nil {
				response.Body.Close()
				return
			}
			select {
			case <-ctx.Done():
				return
			default:
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("request did not start")
	}
	cancel()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "shutdown") {
			t.Fatalf("expected shutdown error, got %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown did not time out")
	}
	<-requestDone
}
