# Publish domain

The publish domain replaces the `publisher` service for TiUP packages (and, as
a sibling kind, image copies). It is served by `cmd/tibuild` and executed by
`cmd/tibuild-worker`.

All of the routes below belong to a single Goa service (`publish`): TiUP
publication is publishing, so it shares the service, the error types and the
`publishsrvc` implementation with the image planner rather than living in a
parallel service.

## Model

| Table | Meaning |
| --- | --- |
| `publish_unit` | one publication intent: an artifact and the set of targets it must reach. It carries the pinned source digest. |
| `publish_step` | one destination of a unit. A worker claims it with a DB lease and heartbeats while executing. `depends_on` orders steps inside a unit. |
| `publish_lane` | a single-row semaphore per kind (`lane=tiup`). It serializes publication intents, and its lease makes a dead worker recoverable. |

States: unit `queued → running → succeeded | partial | failed | canceled`;
step `blocked → pending → claimed → succeeded | failed | skipped`.

## Why the lane is global

`tiup mirror publish` mutates a signed index (component manifest → snapshot →
timestamp). Two concurrent publishes to the same mirror corrupt it, and the two
mirrors must not be published at the same time either. The lane therefore
belongs to the **unit**, not to the process or to a mirror: the worker that
finishes the prod step makes the staging step claimable, and the worker that
picks it up renews the same lane row. Splitting the lane per mirror would let
two units interleave `prod(A) → prod(B) → staging(A) → staging(B)`.

`publish.tiup_dependencies` seeds `depends_on` (default `tiup-prod →
tiup-staging`). Set it to `{}` to run the mirrors in parallel — the direction is
data, not code.

## Targets

| Target | Mirror | Worker config |
| --- | --- | --- |
| `tiup-prod` | `http://tiup.pingcap.net:8987` | `worker.targets: ["tiup-prod"]` |
| `tiup-staging` | `http://tiup.pingcap.net:8988` | `worker.targets: ["tiup-staging"]` |

Each worker mounts only its own signing key
(`/root/.tiup/keys/private.json`) and declares one target. The mirror URLs come
from `publish.tiup_mirrors` and are baked into the step payload, so a worker
never resolves them at execution time.

Timings for the TiUP kind: `lease: 12m`, `heartbeat: 30s`,
`hard_timeout: 8m`. The lane lease is derived from these (never shorter than
the step lease, and at least `hard_timeout + 2×heartbeat`).

## Failure handling

- **Hard timeout**: `tiup mirror publish` runs under `exec.CommandContext`; a
  hung command is killed at `hard_timeout` and the lane is not held past it.
- **Lost lane**: each execution validates the lane before and after publishing.
  A worker that lost it discards its result instead of overwriting the new
  owner's work.
- **Crashed worker**: the claim loop reaps steps whose lease expired. They are
  requeued with a backoff, or failed after `max_attempts`; dependents are
  skipped and the unit converges.
- **Partial publication**: `PARTIAL` is terminal and never retried
  automatically. Inspect it and resume explicitly:

  ```bash
  curl -s https://do2.pingcap.net/tiup/units/<unit-id> | jq
  curl -s -X POST https://do2.pingcap.net/tiup/units/<unit-id>/resume | jq
  ```

  Resume re-queues only the failed and skipped steps; succeeded steps are left
  alone.

## Idempotency

Before publishing, the executor checks the remote tarball:

- missing → publish, then verify the remote sha256;
- present with the same sha256 → success without publishing;
- present with a different sha256 → **refused** ("refusing to overwrite");
  bump the version instead of overwriting a released package.

The `standalone` flag of the artifact config is deliberately not forwarded: the
pre-migration publisher dropped it and this port preserves that behaviour.

## Configuration

```yaml
publish:
  delivery_config_path: /etc/delivery/delivery.yaml
  tiup_mirrors:
    staging: http://tiup.pingcap.net:8988
    prod: http://tiup.pingcap.net:8987
  tiup_dependencies:        # omit for the default; {} for parallel mirrors
    tiup-prod: tiup-staging
  tiup_binary: tiup         # optional override
```

## Legacy API

The pre-migration routes are preserved externally: the gateway rewrites
`/tiup/*` to `/api/v2/publish/tiup/*`. Inside tibuild-v2 the service owns one
base path, `/api/v2/publish`:

- image planning and unit inspection: `/publish/images`, `/publish/units…`
- TiUP: `/publish/tiup/…`

| Route | Behaviour |
| --- | --- |
| `POST /tiup/delivery-by-rules` | expands the rules and returns one step id per target |
| `POST /tiup/publish-request` | one explicit mirror, optional version override |
| `POST /tiup/publish-request-single` | one package from a tarball URL or an OCI layer |
| `GET /tiup/publish-request/{id}` | unit or step id → `queued/processing/success/failed/canceled` |
| `POST /tiup/reset-rate-limit` | no-op |
| `GET /tiup/units/{id}` | unit with all steps |
| `POST /tiup/units/{id}/resume` | re-queue the failed steps |
