# TiBuild

It provide the CD functions for PingCAP, Welcome bros!
- trigger Dev-build runs for custom requirement.
- publish/deliver artifacts.


## Publish domain

TiUP package publishing and the TiDB Cloud ops/TCMS integrations live here now
(formerly in `publisher`). The queue is `publish_unit` + `publish_step`, guarded
by a single-row lane that keeps the two TiUP mirrors from publishing at the
same time; `PARTIAL` publications need an operator and are resumed explicitly.

See [docs/publish.md](./docs/publish.md) for the model, targets, failure
handling and the legacy `/tiup/*` contract.

## Technologies
+ Backend:
  - Golang & GOA(for design)
  - Database: ENT with TiDB/MySQL.
+ Frontend:
  - [Create-React-App for framework](https://github.com/facebook/create-react-app)
  - [Material-UI/MUI for components](https://github.com/mui-org/material-ui)
  - [Axios for remote procedure call](https://github.com/axios/axios)
+ Deployments: Docker & Kubernetes
+ Some core dependencies:
  - [Github](https://github.com/google/go-github)
  - [Config](https://github.com/jinzhu/configor)

## Quick Start

### Design the backend server

go to [internal/service/design/design.go](./internal/service/design/design.go) and update it.

### Design the database

go to [internal/database/schema/](./internal/database/schema/) and update them.

### Generate code from design

> we need the goa tool

run:
```bash
go generate -x ./...
```

### Run it

> Please prepare a `config.yaml` file before start or you
> can passing the `--config` option when run the server.

```bash
go run ./cmd/server
```

After waiting a few seconds, application is available and can be visited in the browser:[localhost:8080](http://localhost:8080/)

## Worker deployment model

TiBuild v2 runs two binaries:

| Binary | Role | State |
| --- | --- | --- |
| `cmd/tibuild` | HTTP API (Goa), stateless | 2+ replicas |
| `cmd/tibuild-worker` | image-sync executor, lease-based | 1+ replicas per target set |

The API no longer executes image sync tasks. Every environment runs the
standalone image `exp-tibuild-worker` (built from `./cmd/tibuild-worker`);
the chart defaults `worker.enabled=true`. Rollback is a chart/image rollback,
not an in-process fallback.

### Queue semantics

`image_sync_tasks` is the queue. A claim sets `claimed_by`, `lease_until`,
`available_at`, and increments `attempts` in one guarded update. The worker
renews the lease with a heartbeat; every result write requires the same
`claimed_by` token, so an expired owner cannot overwrite the next owner's
result. A reaper returns rows whose `lease_until` has passed to `PENDING`.

Failures retry with exponential backoff (10s, 20s, ... capped at 5min, ±20%
jitter) and become `FAILED` after `worker.max_attempts`.

### Configuration

```yaml
worker:
  id: ""                    # defaults to the hostname
  targets: ["*"]            # registry hosts this worker claims
  concurrency: 4            # polling loops
  poll_interval: "10s"
  lease: "90s"
  heartbeat: "30s"
  max_attempts: 3
  kinds: ["image_copy"]
  metrics_interval: "15s"   # task stats / alert evaluation period
  reap_interval: "30s"      # expired-lease reclaim period
  backoff_base: "10s"       # first retry delay (doubles per attempt)
  backoff_max: "5m"         # retry delay cap
  alerts:
    enabled: true           # false keeps metrics but stops alert delivery
    backlog_after: "10m"
    oldest_pending_after: "30m"
    lease_reclaim_window: "5m"
    dedupe: "1h"
```

The observability keys default to the values that used to be hard-coded, so an
untouched config behaves exactly as before. `metrics_interval` and
`reap_interval` are read when the worker starts; the alert thresholds are
re-applied on config reload.

`worker.targets` must be a subset of the global target catalogue; an unknown
target fails fast at startup. Until the target catalogue lands in
`pkg/delivery`, only `"*"` is accepted.

### Metrics and alerts

The worker serves `/metrics` on its health port (8081):

- `publish_task_backlog{target}`
- `publish_task_oldest_pending_seconds{target}`
- `publish_task_inflight{target}`
- `publish_task_lease_expired_total`
- `publish_task_failed_total{target,reason}`

Alerts reuse the Lark notifier and fire when the backlog has been non-empty
for `worker.alerts.backlog_after` (default 10m), when the oldest PENDING task
is older than `oldest_pending_after` (default 30m), or when a lease was
reclaimed within `lease_reclaim_window` (default 5m). Repeats of the same
condition are suppressed for `dedupe` (default 1h).
`worker.alerts.enabled: false` stops alert delivery but keeps the metrics
above refreshing. Configure `lark.channels` to give the alerts recipients.

Alert evaluation itself is elected through the `alert_leases` table: the
workers of all clusters share one queue, so only the lease holder calls the
notifier. The lease TTL is twice `metrics_interval`, and an evicted holder
loses the election on the next tick instead of double-sending. A worker without
`lark.channels` does not compete for the lease, so a cluster that has no
recipients configured cannot take the lease and silence the others.

Changing recipients, forcing an External Secrets sync and verifying delivery is
documented in [`docs/worker-alerts.md`](./docs/worker-alerts.md); scraping the
metrics per cluster is in [`docs/worker-metrics.md`](./docs/worker-metrics.md)
(prod2 uses the chart's `worker.podMonitor`, tke/gcp have no CRDs).

## Testing

```bash
go test ./...
```

Integration tests (e.g. `internal/service/tests/artifact_test.go`) spin up a
real registry via [testcontainers](https://golang.testcontainers.org/), so they
require a running Docker daemon and are skipped with `go test -short ./...`.

The multi-worker zero-duplicate gate uses a MySQL container and is opt-in:

```bash
TESTCONTAINERS_RYUK_DISABLED=true go test -tags=integration \
  ./internal/service/impl -run TestWorkerMultiReplicaAndLeaseRecoveryMySQL -timeout=600s
```

When using [colima](https://github.com/abiosoft/colima) as the Docker backend
on macOS, set the following env vars so testcontainers can reach the daemon and
mount its socket into containers:

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock \
go test ./...
```

## File Structure

> WIP

## Lark build notifications

Notifications use Card JSON 2.0 (Lark client 7.20 or newer). Image results are
grouped by repository, then platform, then tag. Multi-architecture tags appear
first within each repository. Each platform shows its tags inline as code-styled text. Binary results are grouped by platform and filename; each file and its SHA256 download link share a row,
indented beneath the platform.

Card text is uniformly English and maintained in
`internal/service/impl/lark_templates/devbuild-notify.yaml.tmpl`. Artifact
identifiers and pipeline status values remain unchanged.

Binary links use `tekton.oci_file_download_url`, with the same repository, tag
and file query parameters as the DevBuild API. Without a download service URL,
notifications display filenames without a download hyperlink.

Build Report uses nested Markdown list items: artifact type, repository or
platform, and platform/tag or binary/checksum entries. Image repositories and
tags use inline code spans so underscores and other identifier characters stay
literal. Tags remain on the platform line; each binary filename and SHA256 link
share a list item. Lists wrap naturally on narrow clients.

Binary filename and SHA256 hyperlinks can be right-clicked to copy download
addresses. Status and Request retain their original field/list layout and
horizontal-rule section separators. Report text uses the default font size,
with bold labels and no shaded background blocks.
