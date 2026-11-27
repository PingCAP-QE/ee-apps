# authzd authorization core

This module contains the versioned route registry, the proposed OpenFGA model
v1, the first `authzd` service implementation, and the one-shot relationship
sync job. It is deployed in GCP without a Gateway attachment and receives no
production traffic. The ADRs and model still need owner approval;
the checked-in registry is a three-route TiBuild example, not a complete
production policy. Do not attach Envoy routes to this service or enable
`enforce` until the identity handoff, resource ownership source, model, and
route policy have been reviewed and verified.

## Service

`cmd/authzd` serves Envoy's `envoy.service.auth.v3.Authorization/Check` gRPC
API on port `9002`. It takes the route ID and credential mode from Envoy
context extensions named `route_id` and `identity_mode`; it does not infer
route identity from client headers. Each credential mode selects its own
configured token header, JWKS endpoint, issuer, audience, and canonical
principal prefix. The JWT signature and claims are checked independently;
`X-User-*` and `X-Tenant-*` headers are never used as identity.
For rewritten routes, the ext_authz request path is matched first; an Envoy
route configuration may provide a trusted `upstream_path` context extension as
the rewritten candidate. The Gateway configuration that provides these values
still requires versioned render and runtime verification before use.

The health and metrics HTTP listener defaults to `127.0.0.1:9003` and serves
`/healthz`, `/readyz`, and `/metrics`. Keep this listener private when changing
its address. Audit output is structured JSON on stdout and contains decision
metadata only; raw authorization headers and tokens are not recorded.

The service requires these environment variables:

```sh
AUTHZD_REGISTRY_PATH=/etc/authzd/registry.json
AUTHZD_OPENFGA_URL=http://openfga:8080
AUTHZD_OPENFGA_STORE_ID=<store ULID>
AUTHZD_OPENFGA_MODEL_ID=<pinned model ULID>
AUTHZD_CREDENTIAL_MODES='{"machine":{"header":"authorization","jwks_url":"https://issuer.example/jwks","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/"}}'
```

`AUTHZD_OPENFGA_API_TOKEN` is optional and should come from secret management.
`AUTHZD_RESOURCE_CATALOG` may point to a trusted static JSON map for tests and
local integration. It is not a TiBuild resolver or an authoritative production
catalog. Without it, resource resolution returns an error; unknown resources
never get guessed. Do not enforce dynamic resource rules until a read-only,
authoritative ownership resolver is implemented and verified. This service
version resolves path and query IDs only; it does not parse request bodies, so
body-based rules such as the sample create policy remain observe-only.
`AUTHZD_GRPC_ADDR` defaults to `:9002`; `AUTHZD_PRIVATE_ADDR` defaults to
`127.0.0.1:9003`.

Principal prefixes must be OpenFGA-safe: an identifier may not contain `:` after
the type prefix, so issuers appear as a short token (`user:google/sub/<sub>`,
`workload:google/sub/<sub>`) rather than as a URL. OpenFGA rejects the URL form
with `invalid relation: the 'user' field is malformed`.

An ID that the trusted catalog does not contain denies with reason
`resource_not_found`; only a catalog that cannot answer (unconfigured, backend
failure) is a dependency error. Neither case falls back to the path or body.

Credential extraction accepts exactly one token per header. Every identity
failure is distinguishable in the decision log:

| `reason_code` | Meaning |
| --- | --- |
| `identity_missing` | The configured credential header is absent |
| `identity_invalid` | The header is present but empty, ambiguous, or fails verification |
| `relationship_denied` | Identity verified, relation check failed |
| `resource_not_found` / `route_not_registered` | The catalog or registry has no entry |

A forwarded identity header counts as ambiguous when it carries a comma
(Envoy joins repeated headers with commas), embedded whitespace, or an empty
value. A Gateway that failed to overwrite a client-supplied copy of that header
therefore cannot authorize anything: the value is rejected instead of being
parsed as one token.

`AUTHZD_DECISION_MODE` bounds the strength of every response and defaults to
`enforce`:

| Mode | Behaviour |
| --- | --- |
| `off` | Allow everything. No decision is recorded. |
| `observe` / `shadow` | Allow every request and record the decision that `enforce` would have made, including `route_not_registered`, identity, resolver, OpenFGA and context-policy failures. |
| `enforce` | Rule modes decide; an unmatched route denies. |

Use `observe` when attaching `extAuth` to a live route for the first time: with
`enforce`, a route the registry does not list yet is denied for every path it
serves. At this service `shadow` and `observe` are identical, because Envoy only
reaches the ext_authz filter after its own authentication passed; the
distinction is which traffic reaches the filter, not how the decision is made.

Each rule in the registry also carries `off`, `observe`, `shadow`, or `enforce`; a
weaker global mode wins over a stronger rule mode, and rule modes only decide
when the global mode is `enforce`. Use them only after existing controls are
verified to run before this filter. Dependency errors in
`enforce` return a gRPC error and must be paired with Envoy
`failure_mode_allow: false`. This service does not yet prove that any live
Envoy route can only be reached through the verified Gateway.
An optional `ContextPolicyEvaluator` hook accepts normalized, verified
attributes for future ABAC rules; no OPA implementation is installed.

## Relationship sync

`cmd/tuple-sync` reconciles one reviewed snapshot into OpenFGA. It owns only the
object types and relations passed as `--object-type` and `--relation`, deletes
before it writes, refuses an incomplete snapshot, and refuses to empty its scope
without `--allow-empty`. Run it once per reviewed change or from a scheduled job:

```sh
go run ./cmd/tuple-sync \
  --snapshot ./policies/capability-grants.example.json \
  --object-type capability --relation caller --dry-run
```

`--dry-run` reports the plan as `outcome="planned"` without writing. The reviewed
grants file is kept outside this repository: it maps real subjects to capabilities.

## Registry

`policies/registry-v1.json` is ordinary application configuration, not a
Kubernetes CRD. The parser rejects unknown fields, invalid path templates,
ambiguous equal-priority overlaps, malformed objects, and missing checks.
Matching requires host, route identity, exact method, and a normalized path. It
checks external and upstream rewritten paths explicitly, and a miss has no
fallback. A `Store` only publishes fully parsed immutable snapshots.

Static checks use `object`; resource checks use both `object_from` and
`id_from`. A rule may intentionally have one check or several.

TiBuild rules carry a capability check only: object-level ownership stays in the
service, so the gateway never resolves an owner it cannot see.

Run the registry suite with:

```sh
go test -race -cover ./...
```

The committed model is validated and exercised by OpenFGA CLI v0.8.2 against
the golden decisions in `tests/authorization.fga.yaml`:

```sh
go install github.com/openfga/cli/cmd/fga@v0.8.2 # requires Go 1.26+
fga model validate --file models/authorization.fga
fga model test --allow-external-files --tests tests/authorization.fga.yaml
```

These tests verify the source model locally; they do not publish it or establish
a production model ID. Do not use this model or sample policy for enforcement
until the model and policy receive the required owner approvals.
