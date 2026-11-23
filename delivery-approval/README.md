# delivery-approval

Delivery approval service for the `dl` capability links. It owns:

- the OIDC-protected request form and approval pages (later phases),
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
internal/config/     YAML defaults, validation, hot reload
internal/database/   Ent schemas and generated client
internal/store/      domain model, Store interface, TiDB implementation
internal/metrics/    aggregate counters on the private listener
internal/server/     HTTP surface wiring (form/API placeholder in Phase 1)
```
