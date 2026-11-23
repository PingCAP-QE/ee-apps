# delivery-approval

Delivery approval service for the `dl` capability links. It owns:

- the OIDC-protected request form and approval pages (Phase 2),
- the request/approval state machine and audit trail (Ent + TiDB),
- per-file calls to `dl`'s `POST /api/v1/shares` and `DELETE /api/v1/shares/{id}`,
- encrypted, one-time `download_url` storage and Lark delivery (later phases).

`dl` stays the low-level capability service. This module talks to it over HTTP;
it does not import `dl` and does not share its database.

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
| future `DL_APPROVAL_LARK_*`, `DL_APPROVAL_URL_KEY`, `DL_APPROVAL_DL_ISSUER_SECRET` | later phases |

Both `listen` and `health_address` are separate listeners. Never expose
`health_address` through the Gateway or a Service.

## Database

The runtime role has no DDL. Run the migration Job once with the migration DSN:

```bash
DL_APPROVAL_DATABASE_DSN='dl_approval_migrate:...@tcp(...)/delivery_approval?...' \
  go run ./cmd/migrate
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
cmd/server/          application + private probe listeners
cmd/migrate/         one-shot Ent schema migration
internal/auth/       Google ID-token validation and identity context
internal/config/     YAML defaults, validation, hot reload
internal/web/        HTML form/list/detail, CSRF, CSP, rate limits
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
