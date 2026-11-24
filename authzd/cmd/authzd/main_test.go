package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/lestrrat-go/jwx/v4/jwa"
	"github.com/lestrrat-go/jwx/v4/jwk"
)

type startupWriter struct {
	mu      sync.Mutex
	buffer  bytes.Buffer
	started chan struct{}
	once    sync.Once
}

func (w *startupWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	n, err := w.buffer.Write(p)
	w.mu.Unlock()
	if strings.Contains(string(p), "authzd listeners started") {
		w.once.Do(func() { close(w.started) })
	}
	return n, err
}

func TestRunContextStartsPrivateListenersAndShutsDown(t *testing.T) {
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	publicKey, err := jwk.Import[jwk.Key](&privateKey.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := publicKey.Set(jwk.KeyIDKey, "test-key"); err != nil {
		t.Fatal(err)
	}
	if err := publicKey.Set(jwk.AlgorithmKey, jwa.RS256()); err != nil {
		t.Fatal(err)
	}
	set := jwk.NewSet()
	set.AddKey(publicKey)
	keys, err := json.Marshal(set)
	if err != nil {
		t.Fatal(err)
	}
	jwksServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(keys)
	}))
	defer jwksServer.Close()
	registryFile := t.TempDir() + "/registry.json"
	registryJSON := `{"version":"test","routes":[{"id":"r","host":"example.test","route":"r","method":"GET","path":"/","priority":1,"mode":"observe","checks":[{"object":"capability:test","relation":"read"}]}]}`
	if err := os.WriteFile(registryFile, []byte(registryJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	values := map[string]string{
		"AUTHZD_GRPC_ADDR":        "127.0.0.1:0",
		"AUTHZD_PRIVATE_ADDR":     "127.0.0.1:0",
		"AUTHZD_REGISTRY_PATH":    registryFile,
		"AUTHZD_OPENFGA_URL":      "http://127.0.0.1:8080",
		"AUTHZD_OPENFGA_STORE_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_OPENFGA_MODEL_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_CREDENTIAL_MODES": `{"machine":{"header":"authorization","jwks_url":"` + jwksServer.URL + `","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/"}}`,
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	writer := &startupWriter{started: make(chan struct{})}
	logger := slog.New(slog.NewJSONHandler(writer, nil))
	result := make(chan error, 1)
	go func() { result <- runContext(ctx, logger, func(name string) string { return values[name] }) }()
	select {
	case <-writer.started:
	case <-time.After(5 * time.Second):
		t.Fatal("authzd listeners did not start")
	}
	cancel()
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("runContext() shutdown error = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("authzd did not shut down after context cancellation")
	}
}

func TestLoadResolverRequiresTrustedCatalog(t *testing.T) {
	resolver, err := loadResolver("")
	if err != nil || resolver.Version() != "unconfigured" {
		t.Fatalf("loadResolver() = (%v, %v)", resolver, err)
	}
	if _, err := loadResolver(t.TempDir() + "/missing.json"); err == nil {
		t.Fatal("loadResolver() accepted a missing catalog file")
	}
}
