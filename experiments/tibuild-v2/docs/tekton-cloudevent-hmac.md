# Authenticating tibuild CloudEvents to the Tekton EventListener

`tibuild-v2` triggers devbuild/hotfix PipelineRuns by POSTing CloudEvents to the
Tekton EventListener `el-tibuild` (`http://el-tibuild.ee-cd.svc:8080`). The
EventListener is reachable from any pod in the cluster and NetworkPolicy is not
enforced on our clusters, so the transport itself must be authenticated: without
a signature any pod can forge a devbuild event and drive a CI build with CI
credentials.

tibuild signs the request body with HMAC-SHA256 and sends the signature in the
GitHub webhook header `X-Hub-Signature-256`. The EventListener verifies it with
Tekton's built-in `github` interceptor + `secretRef`; no custom interceptor code
is involved.

## What is signed (exactly)

The signature is computed **at the HTTP transport layer**, over the exact bytes
that are written as the request body. It is not computed over a re-serialized
copy of the event, so it stays correct even if the CloudEvents SDK changes its
wire encoding.

Current wire format, measured by the test server in
`internal/service/impl/tekton_cloudevent_test.go`:

- The CloudEvents SDK (`cloudevents.NewClientHTTP`) sends **binary mode**
  (`Content-Type: application/json`): the CloudEvent envelope travels as `ce-*`
  headers (`ce-id`, `ce-type`, `ce-source`, `ce-subject`, extensions as
  `ce-paramproduct`, ...), and the **request body is only the JSON data
  payload**, for example:

  ```json
  {"ref":"refs/heads/master","after":"<sha>"}
  ```

- The body is **not** the structured CloudEvent envelope (no `specversion` /
  `type` keys in the body).
- Signature: `X-Hub-Signature-256: sha256=<lowercase hex>` where
  `<hex> = HMAC-SHA256(secret, body_bytes)`.

Tekton's `github` interceptor reads the raw HTTP body the EventListener received
(`pkg/sink/sink.go` reads the request body once and hands those bytes to the
interceptor) and verifies it with `go-github`'s `ValidateSignature`, which is
the same format GitHub uses. Any interceptor that rewrites the body **before**
the `github` interceptor would invalidate the signature; put the `github`
interceptor first in the chain (or use it alone).

`X-GitHub-Event` is **not** sent and must not be required: with only
`secretRef` configured the `github` interceptor never reads it, so the listener
must **not** set `eventTypes`. Adding `eventTypes` would make the interceptor
reject these events (tibuild only sends the signature header, and the trigger
filters on `ce-type` anyway).

## Configuration contract (ee-ops)

tibuild reads the shared secret from its configuration file:

```yaml
tekton:
  cloudevent_endpoint: http://el-tibuild.ee-cd.svc:8080
  cloudevent_hmac_secret: {{ .cloudevent_hmac }}
```

- Secret Manager: `gcp_tibuild_cloudevent_hmac` in project
  `pingcap-testing-account`.
- Kubernetes-side value: key `secret` of the `el-tibuild-webhook` Secret.
- The secret is used as a UTF-8 string on both sides; the EventListener's
  `github` interceptor `secretRef` reads the same Kubernetes Secret so the two
  sides cannot drift.
- Never commit a value to this repository, and never log it. tibuild only logs
  the config key name, never the value.
- The existing deployment renders the whole `config.yaml` into the
  `v2-config` Secret (`server.config.secretName` in `charts/tibuild-v2`), so
  the value must be injected into that document; tibuild has no separate
  env/file indirection for this key.
- Rotating the value is hot: `tekton.*` config keys are reloaded by the config
  watcher and the CloudEvent client is rebuilt, so no pod restart is needed
  (see `docs/config-reload.md`).

If the key is empty, tibuild keeps the previous behaviour and sends unsigned
events. Exactly one warning is logged per process:

```text
CloudEvent HMAC secret is not configured: devbuild events are sent to the Tekton EventListener UNSIGNED and cannot be authenticated
```

## Rollout order (must not be inverted)

1. Deploy tibuild with `tekton.cloudevent_hmac_secret` set. The listener does
   not verify anything yet, so builds keep working and the signature header is
   ignored.
2. Verify the header arrives (see below).
3. Only then enable the `github` interceptor (`secretRef`) on `el-tibuild` in
   ee-ops, with **no** `eventTypes`.

Enabling verification first would reject every devbuild event until tibuild is
rolled out.

## Verifying

The signature covers the body that arrives at the listener, so it can be checked
from a debug pod against the untouched listener:

```bash
SECRET=... # same value as the mounted config
BODY='{"ref":"refs/heads/master","after":"0000000000000000000000000000000000000000"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)"
curl -si -X POST http://el-tibuild.ee-cd.svc:8080 \
  -H 'Content-Type: application/json' \
  -H 'ce-specversion: 1.0' \
  -H 'ce-id: manual-check' \
  -H 'ce-source: manual-check' \
  -H 'ce-type: manual-check' \
  -H "X-Hub-Signature-256: $SIG" \
  --data-binary "$BODY"
```

**The HTTP status proves nothing.** The Tekton EventListener returns
`202 Accepted` even when the interceptor later rejects the event (interception
runs asynchronously in the trigger group), so both signed and unsigned requests
look the same to the client. Before the interceptor is enabled, confirm the
`X-Hub-Signature-256` header leaves tibuild from its logs/debug tooling; after
it is enabled, confirm acceptance or rejection in the ee-ops core-interceptor
logs and by a real devbuild succeeding.
