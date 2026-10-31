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

### Lane lifecycle

A unit acquires the lane when one of its steps starts executing and releases it
as soon as the unit reaches a terminal state (`succeeded`, `partial`, `failed`,
`canceled`); the next unit can then acquire immediately instead of waiting for
the lease to expire. `retry` needs no special handling: the terminal unit has
already released the lane, and the re-queued step acquires it again.

Finding the lane busy is **not** an execution attempt. The claim increments
`publish_step.attempts`, the requeue decrements it again, so `max_attempts`
only counts real execution failures and timeouts. A busy requeue backs off for
`30s ± 20%` per target, and the wait is exported as `publish_lane_wait_seconds`
alongside the `publish_lane_busy_total` counter.

Known limitation: a unit with two ordered steps (prod → staging) is executed by
two different worker deployments, one per target. The lane is held by the unit,
so the worker that finished prod keeps it until its lease expires (12m) while
the unit is still running; the staging step then reports the lane busy. Fixing
that needs a holder hand-off inside one unit, which is tracked separately.

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
  automatically. Units are one resource regardless of kind, so inspect and
  retry them through `/publish/units` (port-forward; the public route only
  exposes the legacy TiUP surface):

  ```bash
  kubectl -n tibuild port-forward svc/v2-tibuild-v2 8080:80 &
  curl -s localhost:8080/api/v2/publish/units/<unit-id> | jq
  curl -s -X POST localhost:8080/api/v2/publish/units/<unit-id>/retry | jq
  ```

  Retry re-queues only the failed and skipped steps and re-promotes the ones
  whose dependency is satisfied; succeeded steps are left alone.

## Idempotency

Before publishing, the executor checks the remote tarball:

- missing → publish, then verify the remote sha256;
- present with the same sha256 → success without publishing;
- present with a different sha256 on a **formal `vX.Y.Z`** version → refused
  ("refusing to overwrite formal version"); a released package is immutable, so
  bump the version instead;
- present with a different sha256 on a **non-formal** version — pre/beta/rc,
  nightly, nextgen, rust and branch builds — → overwritten on purpose: those
  channels reuse one version string, so each build replaces the previous
  package.

Only the strict `^v[0-9]+\.[0-9]+\.[0-9]+$` form is treated as a released
version. The per-package outcome is recorded in the step result's `action`:

| action | meaning |
| --- | --- |
| `published` | the remote package was missing and is now published |
| `skipped_existing` | the remote package already had the same sha256 |
| `overwritten_nightly` | a nightly build replaced the previous package |
| `overwritten_non_formal` | a pre/beta/rc/nextgen/rust/branch build replaced the previous package |
| `refused_formal_overwrite` | failure log only: the replacement of a released `vX.Y.Z` was refused |

A package with an empty version is treated as non-formal (overwrite allowed) and
logged as a warning: the planner always sets a version, so an empty one means a
rule/artifact-config anomaly rather than a released package.

The `standalone` flag of the artifact config is deliberately not forwarded: the
pre-migration publisher dropped it and this port preserves that behaviour.

## Nightly convergence

Nightly is a **desired state**, not a queue. With `publish.nightly.enabled`
set and the target listed under `publish.nightly.targets`, a nightly push only
upserts `publish_desired` (one row per `target/pkg/os/arch`, newest wins) and a
window-aligned tick publishes the newest artifact once. A push that arrives
while a unit is running replaces the desired fields without touching the
in-flight unit, so the next tick publishes it.

Each target has its own window and anchor (`publish.nightly.default` plus
`publish.nightly.targets.<target>` overrides): staging can converge hourly while
prod converges twice a day. The window is aligned to the anchor, so the publish
time no longer drifts with the first push. Each package gets a stable hash-based
jitter inside the window, capped at a quarter of the window, so a batch of
nightly builds does not hit the serial TiUP lane at the same instant.

A failure keeps `desired_version` unchanged and records `last_failed_version` /
`last_error`: the first failure retries after `retry_after` (default 5m), later
failures wait for the next window. On success `last_succeeded_version` records
what was actually published, even if a newer push replaced the desired version
while the unit was running.

Nightly convergence is **off by default**; enabling it on staging first keeps
prod on the legacy "publish every push" path until the rollout reaches it.
Deferred pushes create no step, so `POST /tiup/delivery-by-rules` returns an
empty list when every selected target converges.

`POST /tiup/reset-rate-limit` stays as a compatibility no-op; there is no Redis
counter to reset.

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
  nightly:                  # off unless enabled explicitly
    enabled: false
    tick_interval: 1m
    batch: 10
    default:                # schedule for unlisted targets
      window: 12h
      anchor: "02:00"        # interpreted in timezone
      timezone: UTC
      retry_after: 5m
      jitter: 30m
      on_failure: alert      # rollback is not implemented
    targets:                 # per-target overrides
      tiup-staging:
        window: 1h
        anchor: "00:00"
      tiup-prod:
        window: 12h
        anchor: "02:00"
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
| `POST /tiup/reset-rate-limit` | no-op: the legacy Redis nightly throttle does not exist here; the convergence work is owned by the nightly track |

Unit inspection and retry are **kind-agnostic** and live on the shared
resource: `GET /publish/units`, `GET /publish/units/{id}` and
`POST /publish/units/{id}/retry`.
