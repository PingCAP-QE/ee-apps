package impl

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"sync"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	cehttp "github.com/cloudevents/sdk-go/v2/protocol/http"
	"github.com/rs/zerolog"
)

const (
	// hubSignature256Header carries the HMAC-SHA256 signature of the request
	// body. Tekton's built-in `github` interceptor verifies this exact header
	// (through go-github's ValidateSignature), using the GitHub webhook format,
	// so the listener needs no custom interceptor code.
	hubSignature256Header = "X-Hub-Signature-256"
	// hubSignature256Prefix is GitHub's algorithm marker for HMAC-SHA256.
	hubSignature256Prefix = "sha256="
)

// missingHMACSecretWarnOnce keeps the "running unsigned" warning to one line
// per process instead of one per client construction.
var missingHMACSecretWarnOnce sync.Once

// newTektonCloudEventClient builds the CloudEvents HTTP client used to deliver
// devbuild/hotfix events to the Tekton EventListener.
//
// When hmacSecret is not empty every outgoing request body is signed with
// HMAC-SHA256 and the signature is sent in X-Hub-Signature-256. When it is
// empty the client behaves exactly as before (unsigned events) and a one-time
// warning is logged: the deployment is then unauthenticated and any pod that
// can reach the listener can forge a build.
func newTektonCloudEventClient(endpoint, hmacSecret string, logger *zerolog.Logger) (cloudevents.Client, error) {
	opts := []cehttp.Option{cehttp.WithTarget(endpoint)}

	if hmacSecret != "" {
		secret := []byte(hmacSecret)
		opts = append(opts, cehttp.WithRoundTripperDecorator(func(base http.RoundTripper) http.RoundTripper {
			if base == nil {
				base = http.DefaultTransport
			}
			return &cloudeventBodySigner{base: base, secret: secret}
		}))
	} else if logger != nil {
		missingHMACSecretWarnOnce.Do(func() {
			logger.Warn().
				Str("config_key", "tekton.cloudevent_hmac_secret").
				Msg("CloudEvent HMAC secret is not configured: devbuild events are sent to the Tekton EventListener UNSIGNED and cannot be authenticated")
		})
	}

	client, err := cloudevents.NewClientHTTP(opts...)
	if err != nil {
		return nil, fmt.Errorf("create cloud event client: %w", err)
	}
	return client, nil
}

// cloudeventBodySigner is an http.RoundTripper that signs the exact request
// body it forwards.
//
// Signing at the transport layer is deliberate: it covers the bytes that
// actually reach the EventListener, whatever wire encoding the CloudEvents SDK
// picks (binary mode today: `ce-*` headers plus the JSON payload as the body;
// structured mode would put the whole envelope in the body). The listener's
// `github` interceptor hashes the raw request body it received, so signing the
// serialized body is the only way to be byte-exact without duplicating the
// SDK's encoder here.
type cloudeventBodySigner struct {
	base   http.RoundTripper
	secret []byte
}

func (s *cloudeventBodySigner) RoundTrip(req *http.Request) (*http.Response, error) {
	if req == nil {
		return nil, fmt.Errorf("sign cloud event: nil request")
	}

	body, err := bufferRequestBody(req)
	if err != nil {
		return nil, err
	}
	if req.Header == nil {
		req.Header = http.Header{}
	}

	req.Header.Set(hubSignature256Header, signCloudEventBody(s.secret, body))

	return s.base.RoundTrip(req)
}

// bufferRequestBody reads the request body, replaces it with a replayable copy
// and returns the bytes that will be sent, so the caller can sign them.
func bufferRequestBody(req *http.Request) ([]byte, error) {
	if req.Body == nil {
		return nil, nil
	}

	body, err := io.ReadAll(req.Body)
	_ = req.Body.Close()
	if err != nil {
		return nil, fmt.Errorf("read cloud event request body for signing: %w", err)
	}

	req.Body = io.NopCloser(bytes.NewReader(body))
	req.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
	req.ContentLength = int64(len(body))

	return body, nil
}

// signCloudEventBody returns the GitHub-compatible signature of body:
// `sha256=<lowercase hex>` of HMAC-SHA256(secret, body).
func signCloudEventBody(secret, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	// hash.Hash.Write never returns an error.
	_, _ = mac.Write(body)
	return hubSignature256Prefix + hex.EncodeToString(mac.Sum(nil))
}
