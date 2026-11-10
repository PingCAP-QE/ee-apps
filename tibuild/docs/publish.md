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

A step acquires the lane when it starts executing and releases it as soon as
the step finishes (success, failure, timeout, or lost lease). The same unit may
re-enter its own live lane, so the second step of an ordered prod → staging unit
starts immediately after the first step succeeds instead of waiting for the
lease. The unit-terminal release is kept as an idempotent backstop.

The lane lease is derived from the worker timings: it is never shorter than the
step lease and never shorter than `hard_timeout + 2×heartbeat`. With
`lease: 9m`, `hard_timeout: 7m` and a 30s heartbeat it is 9m. The lease only
matters when a worker dies mid-step; the next owner can reclaim it after the
lease expires. A step that keeps a healthy heartbeat is never preempted.

Finding the lane busy is **not** an execution attempt. The claim increments
`publish_step.attempts`, the requeue decrements it again, so `max_attempts`
only counts real execution failures and timeouts. A busy requeue backs off for
`30s ± 20%` per target, and the wait is exported as `publish_lane_wait_seconds`
alongside the `publish_lane_busy_total` counter.

Nightly steps age by one priority point per minute of queue wait, capped at 19
points: a p10 nightly step overtakes p20 API work after 10 minutes but never
overtakes a p30 release. `publish_lane_hold_seconds` and
`publish_lane_starvation_seconds` expose the hold time and the oldest pending
wait per target/priority.

## Targets

| Target | Mirror | Worker config |
| --- | --- | --- |
| `tiup-prod` | `http://tiup.pingcap.net:8987` | `worker.targets: ["tiup-prod"]` |
| `tiup-staging` | `http://tiup.pingcap.net:8988` | `worker.targets: ["tiup-staging"]` |

Each worker mounts only its own signing key
(`/root/.tiup/keys/private.json`) and declares one target. The mirror URLs come
from `publish.tiup_mirrors` and are baked into the step payload, so a worker
never resolves them at execution time.

Timings for the TiUP kind: `lease: 9m`, `heartbeat: 30s`,
`hard_timeout: 7m`. `hard_timeout` must stay below `lease*0.8`, and the lane
lease derived from these is 9m (never shorter than the step lease, and at least
`hard_timeout + 2×heartbeat`).

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

## Queue deduplication

Immediate publications are deduplicated **at enqueue**, not when the worker
starts. Pushes of one mutable tag (branch builds, `pre`/`beta`/`rc`, `rust`,
`nextgen`) would otherwise queue one unit each and pay the download and the
lane time again for a version that is about to be replaced.

The unit of deduplication is the **channel**: the sorted set of target steps
plus the exact package set (`<target>|<name>|<version>|<os>|<arch>`). Two units
share a channel only when they publish the same files to the same mirrors; a
unit is never cancelled because one package happens to overlap. Nightly units
are excluded — their latest-wins schedule lives in `publish_desired` (see
[Nightly convergence](#nightly-convergence)).

| Situation | Behaviour |
| --- | --- |
| same channel, same digest | idempotent: return the queued unit's step ids, create nothing |
| non-formal, newer artifact | create the new unit, cancel the older idle units |
| non-formal, older artifact (delayed retry) | ignored: return the queued unit, create nothing |
| formal `vX.Y.Z`, different digest | `409 Conflict`, nothing created or cancelled |
| formal `vX.Y.Z`, older artifact | `409 Conflict` stating which artifact is newer |

"Newer" means the artifact build time (`org.opencontainers.image.created` from
the OCI manifest, carried in the step payload as `artifact_created_at`), not the
queue time: a delayed retry of an old build must not displace a newer one. Units
created before the field existed fall back to `publish_unit.created_at`.

### Cancelling a superseded unit

Only **idle** units are cancelled. A unit with a `claimed` or `succeeded` step
keeps running: it is doing irreversible work, and the new unit publishes after
it (or is refused by the formal guard above). Cancellation runs in one
transaction and aborts if a step was claimed in the meantime. It sets

- `publish_unit.state = canceled` with `finished_at`;
- every `pending` / `blocked` step to `skipped`, with
  `last_error = superseded by <new unit id>` and `finished_at`;
- a `publish unit superseded` log line with `superseded_by`.

Nothing is deleted: the cancelled unit stays as an audit record.

### Formal version conflicts

A released `vX.Y.Z` is immutable, so two different digests for the same channel
cannot be ordered by time. The request fails instead of silently picking the
newer one:

```json
{
  "code": 409,
  "message": "formal version v8.5.6 conflicts with queued unit <id>: existing digest sha256:…, new digest sha256:…",
  "version": "v8.5.6",
  "existingDigest": "sha256:…",
  "newDigest": "sha256:…",
  "existingUnitId": "<id>"
}
```

The same channel also raises a `[tibuild] formal version conflict` alert through
the notifier, deduplicated for one hour per channel, so the conflict is not
visible only to whoever made the HTTP call. Resolve it by deciding which digest
is the release (bump the version for a legitimate rebuild); retrying the request
unchanged will fail again.

The execution-time `refusing to overwrite formal version` check stays as the
last line of defence for anything that reaches a worker.

### Download precheck

Before downloading a package the worker compares the OCI layer digest with the
remote tarball's sha256. They are the same bytes, so a match returns
`skipped_existing` without fetching the layer — a 574 MB package that is already
published costs one HTTP `GET` on the mirror instead of the full transfer. The
remote file still has to be read once to hash it; true zero-transfer needs a
checksum sidecar or `HEAD`-with-digest support on the mirror.

### Scope and limits

- Applies to `POST /tiup/delivery-by-rules` and `POST /tiup/publish-request`.
- `POST /tiup/publish-request-single` is not deduplicated: its digest is a tag,
  not a content digest, so two requests cannot be proven to carry the same file.
- Concurrent enqueues are best-effort: two requests arriving together may both
  create a unit, and the later one cancels the earlier one. The queue converges
  without a unique index.
- Rollback is `git revert` of the change. Cancelled units are not restored
  automatically: re-enqueue the newest artifact for the channel, and use
  `last_error = superseded by …` to trace which unit replaced which.

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

Everything under `publish` except `targets` and `tiup_binary` is applied without
a restart: a new window, a new target list or a new rules path takes effect
within 15s. See [`docs/config-reload.md`](./config-reload.md) for the full list of
hot keys, the restart-only keys and the reload metrics.

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

## TiDB Cloud integrations

Three `publisher` endpoints moved over unchanged in behaviour:

| Legacy route | tibuild-v2 route | Upstream |
| --- | --- | --- |
| `POST /tidbcloud/devops/cloudconfig/versions/component` | `POST /api/v2/tidbcloud/devops/cloudconfig/versions/component` | ops platform (`x-api-key` per stage) |
| `POST /tidbcloud/tidbx-component-image-builds` | `POST /api/v2/tidbcloud/tidbx-component-image-builds` | TCMS (bearer token) |
| `POST /tidbcloud/sync-kernel-images` | `POST /api/v2/tidbcloud/sync-kernel-images` | ops kernel image build callback |

The ops and TCMS configs are mounted from a Secret and referenced by path, so
the API keys never appear in the main config document:

```yaml
tidbcloud:
  ops_config_file: /etc/tidbcloud/ops.yaml
  testplatforms_config_file: /etc/tidbcloud/testplatforms.yaml
```

Reloading the configuration file re-reads both; a bad revision keeps the last
good one. The ops file carries the per-stage base URL, key and the
`base_image → component` catalogue; the test platforms file carries the TCMS
base URL and token.

### Commit metadata forwarded to TCMS

`POST /api/v2/tidbcloud/tidbx-component-image-builds` also reads two optional
labels from the image config and forwards them together with
`repo`/`branch`/`sha`/`imageTag`:

| Label | TCMS field | Rules |
| --- | --- | --- |
| `net.pingcap.tibuild.git-commit-title` | `commitTitle` | trimmed, truncated to 255 runes, omitted when empty |
| `net.pingcap.tibuild.git-commit-time` | `commitTime` | must parse as RFC 3339; otherwise only this field is dropped and a warning is logged |

Both fields are optional end to end. An image built before the labels existed
(or without them) produces exactly the old request shape, and TCMS keeps the
values it already stored when a later update omits the fields. The labels
themselves are written by the QE artifact build scripts — see
[`PingCAP-QE/artifacts`](https://github.com/PingCAP-QE/artifacts) — so the
values describe the commit that produced the image, not the caller.
