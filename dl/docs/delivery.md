# Anonymous OCI file delivery

Delivery is opt-in. The default `--server-mode=internal` continues to mount only
the existing OCI, KS3 and GCS endpoints. TiBuild, CI and Lark links remain unchanged.
The existing `prow.tidb.net/dl` route must retain its Google OIDC SecurityPolicy.

## Separate server surfaces

| Mode | Application port | Private operational port |
| --- | --- | --- |
| `internal` (default) | Existing downloads and probes | Unchanged |
| `management` | Authenticated POST/GET/DELETE `/api/v1/shares` | `/healthz`, `/livez`, `/metrics` |
| `public` | Anonymous GET/HEAD `/d/{token}` only | `/healthz`, `/livez`, `/metrics` |
| `delivery-migrate` | None; apply schema and exit | None |

Run management and public as separate Deployments/Services. Both use the same
TiDB database and policy configuration, but distinct least-privilege DB
roles. Each delivery process loads only OCI credentials; KS3/GCS configuration
is unnecessary. `--delivery-config` is required for both server modes. Startup
fails if the DB is unavailable or the schema has not been initialized.

Delivery mode flags:

| Flag / environment | Default | Meaning |
| --- | --- | --- |
| `DL_DELIVERY_DATABASE_DSN` | Required | TiDB MySQL-protocol DSN; supply through a Secret, include `parseTime=true&loc=UTC`, and use the approved production TLS mode |
| `--delivery-config` | Required | YAML issuer policies and public HTTPS origin |
| `--delivery-health-address` | `:8001` | Private probes and metrics; do not add this port to the public Service/route |
| `--delivery-concurrency` | `32` | Non-queued in-flight request limit per replica |
| `--delivery-rps` / `--delivery-burst` | `10` / `20` | Per-replica request rate; supplement with global edge controls |
| `--delivery-transfer-timeout` | `24h` | Total upstream transfer deadline; downstream stalls have a two-minute write deadline per chunk |

`--debug` is rejected in all delivery modes. They bypass the legacy request ID,
request URL, HEAD interception and body logging middleware. Do not add request
logging/tracing outside these handlers without redacting tokens and issuer
Authorization headers. The CLI also rejects verbose flags for share management.

## Database bootstrap and permissions

Run once with a migration identity, before either new Deployment starts:

```sh
# Supply the TiDB DSN securely through the environment, not process args.
dl --server-mode=delivery-migrate
```

The [Ent schemas](../internal/database/schema) define the grant, issuer status
and separate audit tables; migrations and ordinary CRUD also use Ent. The
session-reservation path keeps a narrow SQL boundary for TiDB's explicit
`BEGIN PESSIMISTIC` and `SELECT ... FOR UPDATE` semantics. TiDB DDL is not
transactional, so run one migration job to completion before starting either
server mode. Normal runtime startup does not run DDL.

Enroll authorized issuers explicitly:

```sql
INSERT INTO download_issuers(id, enabled) VALUES ('release-service', true);
-- Emergency shared disable: denies creation and all NEW redemptions on every replica.
UPDATE download_issuers SET enabled = false WHERE id = 'release-service';
```

Grant the runtime users access to the delivery database, then:

| Role | Table privileges |
| --- | --- |
| Management | SELECT on `download_issuers`; SELECT, INSERT and UPDATE on `download_shares`; INSERT on `download_events` |
| Public | SELECT on `download_issuers` and `download_shares`; UPDATE of `download_shares.sessions_started`; INSERT on `download_events` |

Neither runtime role should modify issuer status, grant schemas/roles, delete
audit events or run migrations. Use an operator identity for retention and
issuer lifecycle. The public role needs UPDATE only for atomic GET reservations,
including unlimited-session counters. Both roles must connect to the TiDB primary
SQL endpoint, not a stale-read or replica endpoint.

Shares and events survive pod restarts. Use database backups and explicit event
retention/access controls. Events contain grant IDs, fixed phase/outcome labels,
timestamps and server-side byte counts; they omit tokens, hashes, full URLs,
headers, IP addresses and upstream error text. Grant rows contain issuer and
customer references; restrict access accordingly.

## Issuer policy and authorization

Start with [delivery-policy.yaml](../examples/delivery-policy.yaml). Generate a
separate random secret of at least 32 bytes for each trusted issuer. Deliver the
secret securely to that service and put only its SHA-256 hash in the mounted
policy. The policy requires:

- An explicit issuer ID and unique credential hash.
- Exact repositories, or an explicit trailing-slash namespace prefix. Prefixes
  cannot match sibling names such as `releases-evil`.
- An allowed-tag regular expression, matched against the entire OCI tag.
- A maximum TTL from one second to 30 days. An omitted request TTL defaults to
  the smaller of 24 hours and the issuer's limit.

The registry host must also pass the existing OCI registry allowlist. No regex
file selection is exposed. Issuance requires an exact, unique layer title, a
valid OCI image manifest and a valid blob digest/size, without external
descriptor URLs or inline data. The issued grant pins the resolved manifest and
blob digest, size, media type and filename. Subsequent requests never resolve
the source tag again. The upstream stream verifies the stored digest and size
with a bounded buffer; late verification failure aborts the transfer and is
audited, but cannot retract bytes already sent.

The trusted release/delivery service must establish release eligibility and
customer entitlement before issuance. The repository and tag policies are
additional restrictions, not a customer identity system. `customer_id` is an
audit attribute, not proof of who holds the anonymous capability.

Policy changes require rolling out the same configuration to both Deployments.
Use durable issuer disable before narrowing/removing an issuer if immediate
cross-replica denial is necessary. Credential rotation does not invalidate old
grants; disable the issuer or revoke individual grants to withdraw them.

## Management API

Use TLS and a private route, optionally gateway machine JWT authentication, in
addition to application authentication. All methods require
`Authorization: Bearer <issuer-secret>`. The authenticated issuer owns the grant;
other issuers cannot inspect or revoke it. OIDC headers or caller-provided issuer
names are never accepted as authority.

```http
POST /api/v1/shares
Authorization: Bearer <issuer-secret>
Content-Type: application/json

{
  "artifact": {
    "repository": "hub.pingcap.net/pingcap/tidb/package",
    "tag": "v8.1.0_linux_amd64",
    "file": "tidb.tar.gz"
  },
  "expires_in_seconds": 86400,
  "purpose": "customer-delivery",
  "customer_id": "customer-123"
}
```

The `201` response includes `id`, `download_url`, timestamps and pinned artifact
metadata. The plaintext URL is returned only here; store or deliver it securely.
`GET /api/v1/shares/{id}` returns metadata/status without the URL/token/hash.
`DELETE /api/v1/shares/{id}` revokes the link and returns `204` (idempotent for
the owner). Responses and errors set `Cache-Control: no-store`.

Optional `max_sessions` is a positive 32-bit integer. One session means one
authorized GET start, including retries and failed/disconnected upstream or
downstream attempts. Reservations and authorization audit are committed in one
explicit TiDB pessimistic transaction. The grant row is read with `FOR UPDATE`
before incrementing the counter, so concurrent requests across replicas cannot
consume the same final session. HEAD applies the same expiry, issuer, revocation
and exhausted-quota checks but consumes no session. This is not exactly-once delivery.

## Redemption semantics

GET or HEAD the exact `/d/{token}` URL. Tokens have 256 random bits, URL-safe
base64 encoding, and only SHA-256 hashes are persisted. Queries, escaped paths,
extra segments and all other endpoints are rejected with a generic error.
Unknown, expired, revoked, disabled-issuer and quota-exhausted grants return the
same non-disclosing `404`; database failures return `503` and upstream failures
return `502`, without upstream details. Responses use no-store/no-referrer,
nosniff, a safely encoded attachment filename and binary Content-Type.

Revocation and expiration prevent new authorizations after the corresponding
shared-state change is visible. Already authorized streams may finish. Expiry
uses database time, avoiding per-replica clock drift. GET streams with a 32 KiB
buffer; HEAD does not fetch blob contents. HTTP Range/If-Range are not supported;
Range headers receive the full `200` representation and consume a GET session.
Server-side `stream_finished` means the server finished writing the stream, not
proof of complete receipt by the customer.

Metrics on the private operational port expose aggregate issuance, token/auth
denial, active-transfer, failed-transfer and byte counters. Audit rows record
issued/revoked grants, authorization decisions and transfer start/end outcomes.
Never add token/URL/customer values to metric labels. Rate/concurrency limits
are per replica; configure global edge limits for production.

## Deployment and rollout in ee-ops

The Helm chart accepts `server.env`/`server.envFrom` for Secret references. Use
[public-values.yaml](../examples/public-values.yaml) for a new public release;
overlay [management-values.yaml](../examples/management-values.yaml) for another
internal management release. Update the image tag, policy, credentials, database
roles and service account identity for the target environment. Routes are
deliberately disabled in both examples.

Before adding a public route in `ee-ops/apps/gcp/dl`:

1. Keep the current internal `/dl` HTTPRoute and Google OIDC SecurityPolicy.
   Verify their Accepted/Enforced status and live unauthenticated redirect/deny
   behavior, including existing TiBuild GET/HEAD links.
2. Provision or select the shared TiDB database, run the migration, enroll the
   chosen issuer, mount policies and read-only registry credentials, then deploy
   the new modes without public routing. Confirm schema readiness and private metrics.
3. Use a separate TLS hostname/HTTPRoute that matches only `/d/` and targets the
   **public-mode Service**. Set the large-download request timeout to `0s`; apply
   edge rate limits. Never route management, legacy endpoints or the operational
   port through this hostname. Do not put an OIDC callback on the public route.
4. Redact the entire token path from Gateway access logs, traces, analytics and
   error reporting before canary access. Redact issuer headers and management
   response bodies too. Application logging alone cannot enforce proxy redaction.
5. Restrict network access to the public application port from the Gateway,
   management from authorized callers, and operational ports from probes/metrics.
   Restrict DB and registry access with NetworkPolicy and upstream permissions.
6. Test a canary host: valid anonymous GET/HEAD; expiry/revocation/issuer disable;
   tag repush; query/filename substitution; and all legacy/management paths with
   path/hostname variations. Verify HTTPRoute acceptance and OIDC isolation live.
7. Align OCI retention/GC with the longest grant window, or copy approved delivery
   artifacts into immutable retained storage. Digest pinning cannot prevent GC.
8. Enable the production route only after these checks. Rollback by removing the
   public route or disabling issuers; the internal OIDC workflow is independent.

Live cluster checks, customer entitlement integration, hostname/TLS provisioning,
Gateway log redaction and retention configuration must be completed in ee-ops;
this repository change does not claim those checks have run.

## Validation

```sh
cd dl
go test -race ./...
go build ./...
go generate ./...
git diff --exit-code -- gen internal/database/ent
```

Set `DL_TEST_TIDB_DSN` to a disposable TiDB DSN to enable integration tests.
They create/drop isolated databases and verify quota races across two
independent connection pools, HEAD behavior, shared revocation, expiry, disabled
issuers, ownership, restarts and idempotent migrations. CI runs these tests with
TiDB v8.5.8. HTTP tests cover route isolation, pinned identity, safe errors,
headers/logs, stream failures and a synthetic 5 GiB bounded-buffer transfer.

Deferred: resumable Range transfers, global bandwidth/customer quotas, active
stream cancellation on revoke, customer login/self-service and whole-image OCI
Distribution API pulls.
