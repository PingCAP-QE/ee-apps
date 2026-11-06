package impl_test

import (
	"context"
	"testing"

	gh "github.com/google/go-github/v93/github"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// TestCreateSignsCloudEventBody verifies the wiring end to end: a secret in
// `tekton.cloudevent_hmac_secret` makes the devbuild service sign every
// outgoing CloudEvent, and the signature validates against the raw body the
// listener received (the bytes Tekton's `github` interceptor hashes).
func TestCreateSignsCloudEventBody(t *testing.T) {
	const secret = "e2e-shared-secret"

	env := setupTestEnv(t, func(cfg *config.Service) {
		cfg.Tekton.CloudeventHmacSecret = secret
	})
	defer teardownTestEnv(env)

	env.github.reset()
	env.cloudEvents.reset()

	_, err := env.service.Create(context.Background(), gitRefCreatePayload("hmac-signer@test", "branch/master", false))
	require.NoError(t, err)
	require.Equal(t, 1, env.cloudEvents.count())

	body := env.cloudEvents.body()
	header := env.cloudEvents.header()
	signature := header.Get("X-Hub-Signature-256")

	require.Regexp(t, `^sha256=[0-9a-f]{64}$`, signature)
	require.NoError(t, gh.ValidateSignature(signature, body, []byte(secret)))

	// The signed bytes are the binary-mode body (the JSON data payload), not
	// the CloudEvent envelope.
	require.NotContains(t, string(body), "specversion")
	require.Equal(t, "net.pingcap.tibuild.devbuild.push", header.Get("ce-type"))
}
