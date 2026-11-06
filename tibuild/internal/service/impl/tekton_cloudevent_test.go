package impl

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	ceprotocol "github.com/cloudevents/sdk-go/v2/protocol"
	gh "github.com/google/go-github/v93/github"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
)

// TestSignCloudEventBody_RFC4231Vector pins the signature to the RFC 4231
// HMAC-SHA256 test vector (test case 2), so a refactor cannot silently change
// the algorithm, the key handling or the hex encoding.
func TestSignCloudEventBody_RFC4231Vector(t *testing.T) {
	got := signCloudEventBody([]byte("Jefe"), []byte("what do ya want for nothing?"))
	const want = "sha256=5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
	require.Equal(t, want, got)
}

// TestSignCloudEventBody_IndependentVerification checks the GitHub-compatible
// format and verifies the signature with two implementations that do not share
// code with signCloudEventBody: an RFC 2104 HMAC-SHA256 written out here, and
// go-github's ValidateSignature — the same function Tekton's `github`
// interceptor calls.
func TestSignCloudEventBody_IndependentVerification(t *testing.T) {
	secret := []byte("test-shared-secret")
	body := []byte(`{"ref":"refs/heads/master","after":"6f2a1c"}`)

	got := signCloudEventBody(secret, body)

	require.Regexp(t, `^sha256=[0-9a-f]{64}$`, got)
	require.Equal(t, "sha256="+hex.EncodeToString(manualHMACSHA256(secret, body)), got)
	require.NoError(t, gh.ValidateSignature(got, body, secret))

	// Any byte out of place must invalidate the signature.
	require.Error(t, gh.ValidateSignature(got, append(body, ' '), secret))
	require.Error(t, gh.ValidateSignature(got, body, []byte("another-secret")))
	require.Error(t, gh.ValidateSignature(strings.Replace(got, "sha256=", "sha1=", 1), body, secret))
}

// TestTektonCloudEventClient_SignsExactWireBody is the end-to-end proof of what
// the EventListener receives: the client is pointed at a real HTTP server, the
// captured raw request body is what the signature is computed over, and the
// capture also records the CloudEvents wire mode (binary: `ce-*` headers plus
// the JSON data payload as the body — not the structured envelope).
func TestTektonCloudEventClient_SignsExactWireBody(t *testing.T) {
	const secret = "wire-level-shared-secret"

	server, captured := newCloudEventRecordingServer(t)
	logger := zerolog.Nop()
	client, err := newTektonCloudEventClient(server.URL, secret, &logger)
	require.NoError(t, err)

	event := newTestCloudEvent(t)
	wantBody := event.Data()
	require.NotEmpty(t, wantBody)

	_, result := client.Request(context.Background(), event)
	require.True(t, ceprotocol.IsACK(result), "request must be acknowledged, got %v", result)

	rec := captured()

	// 1. Signature covers the exact bytes that arrived at the listener.
	signature := rec.header.Get(hubSignature256Header)
	require.Regexp(t, `^sha256=[0-9a-f]{64}$`, signature)
	require.Equal(t, "sha256="+hex.EncodeToString(manualHMACSHA256([]byte(secret), rec.body)), signature)
	require.NoError(t, gh.ValidateSignature(signature, rec.body, []byte(secret)))

	// 2. Wire format: binary CloudEvent mode. The body is exactly the JSON
	//    payload the event carries; the CloudEvent envelope travels as headers.
	require.Equal(t, wantBody, rec.body)
	require.NotContains(t, string(rec.body), "specversion")
	require.Equal(t, "application/json", rec.header.Get("Content-Type"))
	require.Equal(t, event.Type(), rec.header.Get("ce-type"))
	require.Equal(t, event.ID(), rec.header.Get("ce-id"))
	require.Equal(t, event.Source(), rec.header.Get("ce-source"))

	// 3. Only the signature header is added: the `github` interceptor does not
	//    read X-GitHub-Event when the listener has no `eventTypes` (and the
	//    listener must not configure them), so no synthetic event is sent.
	require.Empty(t, rec.header.Get("X-GitHub-Event"))
}

// TestTektonCloudEventClient_NoSecretPreservesBehaviour covers the rollout
// state where the secret is not (yet) mounted: no signature header is added,
// the wire bytes are untouched, and a single warning names the config key.
func TestTektonCloudEventClient_NoSecretPreservesBehaviour(t *testing.T) {
	missingHMACSecretWarnOnce = sync.Once{}

	server, captured := newCloudEventRecordingServer(t)
	var logs bytes.Buffer
	logger := zerolog.New(&logs)
	client, err := newTektonCloudEventClient(server.URL, "", &logger)
	require.NoError(t, err)

	event := newTestCloudEvent(t)
	_, result := client.Request(context.Background(), event)
	require.True(t, ceprotocol.IsACK(result), "request must be acknowledged, got %v", result)

	rec := captured()
	require.Empty(t, rec.header.Get(hubSignature256Header), "unsigned client must not invent a signature")
	require.Equal(t, event.Data(), rec.body)

	require.Contains(t, logs.String(), "tekton.cloudevent_hmac_secret")
	require.NotContains(t, logs.String(), "sha256=")
}

// TestTektonCloudEventClient_DoesNotLogSecret ensures neither the successful
// nor the failed send path can leak the shared secret into logs.
func TestTektonCloudEventClient_DoesNotLogSecret(t *testing.T) {
	const secret = "super-secret-value-that-must-never-be-logged"

	var logs bytes.Buffer
	logger := zerolog.New(&logs)

	server, _ := newCloudEventRecordingServer(t)
	client, err := newTektonCloudEventClient(server.URL, secret, &logger)
	require.NoError(t, err)
	_, result := client.Request(context.Background(), newTestCloudEvent(t))
	require.True(t, ceprotocol.IsACK(result), "request must be acknowledged, got %v", result)

	// Transport error path: a closed server makes the CloudEvents client report
	// a non-ACK result, which triggerTknBuild logs.
	closed := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	closed.Close()
	failingClient, err := newTektonCloudEventClient(closed.URL, secret, &logger)
	require.NoError(t, err)
	_, result = failingClient.Request(context.Background(), newTestCloudEvent(t))
	require.False(t, ceprotocol.IsACK(result))

	require.NotContains(t, logs.String(), secret)
}

// TestCloudeventBodySigner_SignsAndReplaysRawBody proves the signer covers
// arbitrary body bytes (it must not assume JSON or a CloudEvent envelope) and
// leaves the request replayable, so signing cannot break the send.
func TestCloudeventBodySigner_SignsAndReplaysRawBody(t *testing.T) {
	secret := []byte("round-trip-secret")
	raw := []byte("\x00 not json at all \r\n")

	var (
		sentBody      []byte
		sentLength    int64
		sawGetBody    bool
		sentSignature string
		extraHeaders  http.Header
	)
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		sentBody, _ = io.ReadAll(req.Body)
		sentLength = req.ContentLength
		sawGetBody = req.GetBody != nil
		sentSignature = req.Header.Get(hubSignature256Header)
		extraHeaders = req.Header.Clone()
		return &http.Response{
			StatusCode: http.StatusAccepted,
			Header:     http.Header{},
			Body:       io.NopCloser(strings.NewReader("")),
		}, nil
	})

	signer := &cloudeventBodySigner{base: base, secret: secret}
	req, err := http.NewRequest(http.MethodPost, "http://listener.example/", bytes.NewReader(raw))
	require.NoError(t, err)

	resp, err := signer.RoundTrip(req)
	require.NoError(t, err)
	require.Equal(t, http.StatusAccepted, resp.StatusCode)

	require.Equal(t, raw, sentBody, "the signer must forward the body unchanged")
	require.Equal(t, int64(len(raw)), sentLength)
	require.True(t, sawGetBody, "the request must stay replayable")
	require.Equal(t, signCloudEventBody(secret, raw), sentSignature)
	require.Empty(t, extraHeaders.Get("X-GitHub-Event"), "the sender must not send a synthetic GitHub event")

	replayed, err := req.GetBody()
	require.NoError(t, err)
	replayedBody, err := io.ReadAll(replayed)
	require.NoError(t, err)
	require.Equal(t, raw, replayedBody)
}

func TestSignCloudEventBody_ChangesWithInput(t *testing.T) {
	secret := []byte("a-secret")
	base := signCloudEventBody(secret, []byte("one"))
	require.NotEqual(t, base, signCloudEventBody(secret, []byte("two")))
	require.NotEqual(t, base, signCloudEventBody([]byte("b-secret"), []byte("one")))
}

// manualHMACSHA256 is an RFC 2104 HMAC-SHA256 implementation independent of
// crypto/hmac, used to verify the production signing helper.
func manualHMACSHA256(key, message []byte) []byte {
	const blockSize = sha256.BlockSize // 64 bytes

	if len(key) > blockSize {
		sum := sha256.Sum256(key)
		key = sum[:]
	}

	padded := make([]byte, blockSize)
	copy(padded, key)

	inner := make([]byte, blockSize)
	outer := make([]byte, blockSize)
	for i := range padded {
		inner[i] = padded[i] ^ 0x36
		outer[i] = padded[i] ^ 0x5c
	}

	innerHash := sha256.New()
	innerHash.Write(inner)
	innerHash.Write(message)

	outerHash := sha256.New()
	outerHash.Write(outer)
	outerHash.Write(innerHash.Sum(nil))

	return outerHash.Sum(nil)
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

type cloudEventListenerRequest struct {
	header http.Header
	body   []byte
}

func newCloudEventRecordingServer(t *testing.T) (*httptest.Server, func() cloudEventListenerRequest) {
	t.Helper()

	var (
		mu  sync.Mutex
		got cloudEventListenerRequest
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		mu.Lock()
		got = cloudEventListenerRequest{header: r.Header.Clone(), body: body}
		mu.Unlock()
		// The Tekton EventListener answers 2xx on accepted events.
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(server.Close)

	return server, func() cloudEventListenerRequest {
		mu.Lock()
		defer mu.Unlock()
		return got
	}
}

func newTestCloudEvent(t *testing.T) cloudevents.Event {
	t.Helper()

	event := cloudevents.NewEvent()
	event.SetID("test-event-id")
	event.SetType("net.pingcap.tibuild.devbuild.push")
	event.SetSource("tibuild.pingcap.net/api/v2/devbuilds/42")
	event.SetSubject("42")
	event.SetExtension("paramProduct", "pd")
	require.NoError(t, event.SetData(cloudevents.ApplicationJSON, map[string]any{
		"ref":   "refs/heads/master",
		"after": "6f2a1c",
	}))
	return event
}
