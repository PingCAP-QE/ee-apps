# delivery-approval

Delivery approval service for the `dl` capability links. It owns:

- the OIDC-protected request form and approval pages (Phase 2),
- the request/approval state machine and audit trail (Ent + TiDB),
- per-file calls to `dl`'s `POST /api/v1/shares` and `DELETE /api/v1/shares/{id}`,
- encrypted, one-time `download_url` storage and Lark delivery (later phases).

`dl` stays the low-level capability service. This module talks to it over HTTP;
it does not import `dl` and does not share its database.

The operator runbook — request lifecycle, approval/revocation, audit queries,
log fields and the Lark/TiDB/`dl` degradation paths — is
[`docs/approval.md`](docs/approval.md).

## Phase 1 scope

This phase adds the service skeleton, configuration and storage:

- private probe listener (`/livez`, `/healthz`, `/metrics`);
- application listener with no business routes yet (all 404) — the form lands in
  the next phase;
- `config` package with defaults, validation and hot reload;
- Ent schemas for `requests`, `request_files` and `request_events`;
- TiDB store with conditional status transitions and an append-only event table;
- a separate migration command.

## Phase 2 scope

- Validate the Google ID token forwarded in `X-Approval-Id-Token` (signature,
  audience, `email_verified`, allowed domain) and never trust other identity
  headers.
- Server-rendered request form (`GET /`), request creation (`POST /requests`),
  my-request list (`GET /requests`) and detail (`GET /requests/{id}`).
- CSRF double-submit cookie plus strict same-origin form action, CSP
  `default-src 'self'`, no inline scripts, `X-Frame-Options: DENY`.
- Per-requester limits: pending count and requests in the last 24 hours.
- Exact, unambiguous artifact input: one repository/tag and 1–10 filenames per
  request; the store keeps one `request_file` per filename.

## Phase 3 scope

- Approver actions on the detail page: approve / reject with a note, and revoke
  for the requester or an approver.
- `GET /pending` for approvers, plus the pending section on `GET /requests`.
- Every approval decision is an atomic store transition: conditional
  `UPDATE ... WHERE status='pending'` plus an append-only `request_event` in the
  same transaction.
- A 60s expiry sweeper moves `pending`/`approved` requests past `expires_at` to
  `expired`; approved requests also revoke their issued shares best-effort.
  The revoker is nil until the dl client phase, so today it only expires the
  request.
- Self-approval and non-approver decisions are rejected in the app, not by the
  route config.

## Phase 4 scope

- `internal/dl`: client for `POST /api/v1/shares` and `DELETE /api/v1/shares/{id}` with
  issuer bearer auth, fixed error classes and retries for 429/5xx/timeouts.
- `internal/secure`: AES-256-GCM sealing for the one-time `download_url`.
- `internal/delivery`: issues one share per file, stores `share_id` + sealed URL,
  builds links by decrypting in memory, retries notification up to 5 times, and
  revokes every issued share + clears sealed URLs on failure.
- Approval now triggers delivery asynchronously (5-minute budget); the Lark
  notifier is added in Phase 5. Until then `Delivery` is nil in the server wiring,
  so this phase does not change runtime behavior.

## Phase 5 scope

- `internal/lark`: outbound-only Lark OpenAPI client (no WebSocket, no event
  subscription, no card callback).
  - approval card containing the review link and no download token; when
    `lark_notification_chat_id` is set it goes to that group, when empty it is
    sent privately to every configured approver;
  - optional app-level urgent (`lark_buzz`) in group mode, with email → open_id
    lookup and a cache; DM mode does not call urgent because the card is
    already a private message; a partial DM failure is logged, and only a
    total failure of every approver is returned as an error;
  - private text message with the one-time delivery links;
  - private rejection/revocation messages and a fixed-class delivery-failure
    message (no upstream error body or token).
- Approval cards are patched after the decision: `request_events` stays the
  audit log, `approval_cards` stores one `(request_id, approver_email,
  message_id)` row per delivered card, and approve/reject/revoke/expire patch
  each card to its decided state so nobody clicks a stale button. Patching is
  best effort (Lark only patches cards sent in the last 14 days) and never
  changes a decision.
- Server wiring enables delivery when `DL_APPROVAL_LARK_APP_ID`,
  `DL_APPROVAL_LARK_APP_SECRET`, `DL_APPROVAL_DL_ISSUER_SECRET` and
  `DL_APPROVAL_URL_ENCRYPTION_KEY` are present. `lark_notification_chat_id` is
  optional: empty means per-approver DM. A partially configured provider fails
  startup.
- The expiry sweeper now uses the same `dl` client as revoker.

## Configuration

```bash
cp config.example.yaml config.yaml
```

The `config.yaml` file is validated strictly (unknown fields are rejected) and
reloaded every 30 seconds when the mounted Secret file changes. Secrets are not
part of this file:

| Environment variable | Purpose |
| --- | --- |
| `DL_APPROVAL_DATABASE_DSN` | TiDB MySQL-protocol DSN for the runtime role |
| `DL_APPROVAL_LARK_APP_ID` / `DL_APPROVAL_LARK_APP_SECRET` | dedicated delivery-approval Lark app |
| `DL_APPROVAL_DL_ISSUER_SECRET` | bearer secret for `dl` management API |
| `DL_APPROVAL_URL_ENCRYPTION_KEY` | base64 32-byte AES-256 key for sealed URLs |

Both `listen` and `health_address` are separate listeners. Never expose
`health_address` through the Gateway or a Service.

`public_url` is the browser-facing base of the pages, and its path is also the
mount point: the HTTP route publishes `/dl/grants`, rewrites that prefix to `/`
before forwarding, and the handler rebuilds it for every link and redirect.
Changing the mount point therefore means changing both the route and
`public_url`, and keeping the two in sync.

## Pages

`public_url` is the browser-facing base, and its path is also the mount point:
the HTTP route publishes `/dl/grants`, rewrites the prefix to `/`, and the
handler rebuilds it for every link and redirect.

The request form can be prefilled from the query string, which lets an existing
`/dl/oci-file/<repository>?file=…&tag=…` link be turned into an approval request
by pointing it at the form:

```text
/dl/grants?repository=us-docker.pkg.dev/project/hub/package&tag=v8.5.4
           &file=tidb.tar.gz&file=tikv.tar.gz&note=customer%20asked
```

- `file` may repeat; `files` also accepts a newline- or comma-separated list.
- `ttl` is **not** read from the query: it always starts at
  `default_ttl_seconds`. In the form it accepts seconds (`86400`) or a Go
  duration with hour/minute/second units (`24h`, `1m`, `30s`, `2h30m`), bounded
  by `max_ttl_seconds`; day and week units are rejected so each TTL has one
  spelling. The request stores seconds and the detail page renders the standard
  duration form (`24h0m0s (86400s)`).
- Unknown or malformed values are ignored; the query never influences
  authorization.

## Database

The runtime role has no DDL. Run the migration Job once with the migration DSN:

```bash
DL_APPROVAL_DATABASE_DSN='dl_approval_migrate:...@tcp(...)/delivery_approval?...' \
  go run ./cmd/server -mode=migrate
```

The server refuses to start when the schema is missing. Database/user creation
is described in the ee-ops `apps/gcp/dl-approval/README.md` runbook.

## Run and test

```bash
go test ./...
go build ./...
# TiDB integration tests (disposable database):
DL_APPROVAL_TEST_TIDB_DSN='root@tcp(127.0.0.1:4000)/test?parseTime=true&loc=UTC' \
  go test -race ./...
```

Ent code is generated from `internal/database/schema`:

```bash
go generate ./internal/database/...
```

## Layout

```text
cmd/server/          server + private probe listeners; -mode=migrate runs the
                     one-shot schema migration in the same image
internal/approval/   Expiry sweeper and share-revoker interface
internal/delivery/   dl issuance + sealed URL delivery orchestration
internal/dl/         dl capability API client
internal/lark/       outbound Lark card/urgent/DM client
internal/secure/     AES-256-GCM sealed URL
internal/auth/       Google ID-token validation and identity context
internal/config/     YAML defaults, validation, hot reload
internal/web/        HTML form/list/detail/pending, approval actions, CSRF, CSP, rate limits
internal/database/   Ent schemas and generated client
internal/store/      domain model, Store interface, TiDB implementation
internal/metrics/    aggregate counters on the private listener
internal/server/     HTTP surface wiring (form/API placeholder in Phase 1)
```

## Design notes

- **Why `internal/store` instead of using Ent types in handlers:** Ent is the
  persistence layer; the store is the transaction/invariant boundary. It owns
  the atomic "request + files + created event" transaction, the conditional
  `UPDATE ... WHERE status='pending'`, the append-only event insert, and the
  encrypted URL lifecycle. Keeping generated Ent types out of the HTTP layer
  also lets the form/approval tests use a fake store instead of a database.
  Later phases will put the approval state machine in a service package that depends
  on this interface.
- **Why not Goa:** the approval surface is server-rendered HTML (form, detail,
  approve/reject) plus a handful of JSON endpoints. Goa's design-first
  server/client codegen is a good fit for the machine APIs in `dl/` and
  `tibuild/`, but adds generated transport code and a DSL without a client here.
  If the service later exposes a stable API for other callers, adding a Goa
  service (or a separate API package) is straightforward.
- **Go and Ent versions:** `go.mod` keeps the language minimum at Go 1.25.8
  because Ent v0.14.6 (the latest release, 2026-03-17) has no newer version and
  its code generator is not compatible with Go 1.27 export data. The
  `toolchain go1.27.3` line makes normal builds/tests use Go 1.27.3; only
  `go generate` pins `GOTOOLCHAIN=go1.25.8` for the Ent loader. When Ent ships a
  generator compatible with Go 1.27, the minimum can be raised and the pin
  removed.
