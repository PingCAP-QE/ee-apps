# Operating nightly convergence

This page is for two audiences:

- **package maintainers**, who need to know when a nightly push becomes a
  published package, and
- **on-call**, who need to inspect, re-drive or disable the schedule.

The design is documented in [`publish.md`](./publish.md#nightly-convergence);
this page is the day-two view.

## What changed for maintainers

Nightly used to be a queue with a rate limiter: the first push inside a window
was published and every later push in the same window was dropped
(`skip: rate limit exceeded`). It is now a **desired state**: every push records
the newest artifact, and one publication happens at each window boundary.

| Before | Now |
| --- | --- |
| first push in the window is published | the **newest** push in the window is published at the boundary |
| later pushes are dropped and never published | later pushes replace the desired version, nothing is lost |
| the window starts at the first push, so the publish time drifts | the window is aligned to an anchor, so the publish time is predictable |
| a failed publish wastes the whole window | a failure retries after `retry_after`, then waits for the next window |
| a push always creates work | a deferred push creates **no** unit; `POST /tiup/delivery-by-rules` returns `[]` |

Consequences worth internalising:

- **A green build no longer implies a published package.** The package appears
  at the next window; `publish_desired.next_run_at` says when.
- **A newer artifact wins.** If the newest nightly build is broken, that broken
  build is what the window publishes; the previous good package is not restored
  automatically (`on_failure: alert`, `rollback` is not implemented). The last
  good publication is still queryable in `last_succeeded_version`.
- **Windows differ per mirror.** `tiup-staging` converges hourly (anchor
  `00:00` UTC), `tiup-prod` twice a day (anchor `02:00` UTC). Each package adds
  a stable jitter inside the window (`sha256(pkg) mod jitter`, capped at a
  quarter of the window) so a batch of builds does not hit the serial TiUP lane
  at the same instant.
- **Publishing stays serial.** Nightly units share the `tiup` lane with release
  and API publications and carry the lowest base priority (`release > api >
  nightly`), with aging so they cannot be starved indefinitely.
- **A worker only reschedules the targets it shards.** The unit aggregator and
  the lease reaper see units of every target, but the window belongs to the
  target: a prod worker that settled a staging row would advance it to the next
  prod boundary and the hourly channel would silently skip eleven windows. Rows
  of a target a worker does not shard are left to that target's worker, whose own
  sweep closes them.

## Reading `publish_desired`

One row per `(target, pkg, os, arch)`:

| Column | Meaning |
| --- | --- |
| `desired_version`, `desired_payload`, `desired_at` | newest artifact pushed for this channel, and when |
| `next_run_at` | when the row becomes publishable: the first aligned boundary after the push, plus the per-package jitter |
| `running_unit_id` | the unit currently publishing this row; `NULL` means idle. A tick only claims idle rows |
| `last_succeeded_version` | the last version actually published |
| `last_failed_version`, `last_error` | the last failure, kept until the next success clears it |
| `created_at`, `updated_at` | bookkeeping |

A push never touches `next_run_at` or `running_unit_id`, so a request that
arrives during a publication waits for the next window instead of tearing the
in-flight unit's parameters.

## Inspection queries

```sql
-- what is due, and how overdue
SELECT target, pkg, os, arch, desired_version, desired_at, next_run_at,
       TIMESTAMPDIFF(SECOND, desired_at, NOW(3)) AS lag_s
  FROM publish_desired
 WHERE running_unit_id IS NULL AND next_run_at <= NOW(3)
 ORDER BY next_run_at;

-- what is claimed, and by which unit
SELECT target, pkg, os, arch, desired_version, running_unit_id, updated_at
  FROM publish_desired
 WHERE running_unit_id IS NOT NULL;

-- what is failing
SELECT target, pkg, os, arch, desired_version, last_failed_version,
       last_error, next_run_at
  FROM publish_desired
 WHERE last_error IS NOT NULL
 ORDER BY updated_at DESC;

-- what is converging per target
SELECT target, COUNT(*) AS rows_pending,
       MIN(next_run_at) AS oldest_due,
       MAX(desired_at)  AS newest_push
  FROM publish_desired
 GROUP BY target;
```

`pkg`, `os`, `arch` are the TiUP package name and platform, for example
`tidb` / `linux` / `amd64`.

## Manual actions

### Publish the newest artifact now

Move the schedule instead of resetting a throttle. Only an idle row is
publishable:

```sql
UPDATE publish_desired
   SET next_run_at = NOW(3), updated_at = NOW(3)
 WHERE target = 'tiup-staging' AND pkg = 'tidb' AND os = 'linux' AND arch = 'amd64'
   AND running_unit_id IS NULL;
```

The tick picks it up within `tick_interval` (1m). When the unit settles,
`next_run_at` returns to the next aligned window, so this does not permanently
change the cadence.

### Re-arm a row after a dead unit

If a worker died mid-step and the reaper could not settle the row, the row stays
claimed and the reconciler skips it. Confirm the unit is terminal first
(`publish_unit.state`), then release the claim:

```sql
-- verify first
SELECT id, state FROM publish_unit
 WHERE id = (SELECT running_unit_id FROM publish_desired
              WHERE target = 'tiup-prod' AND pkg = 'tidb'
                AND os = 'linux' AND arch = 'amd64');

UPDATE publish_desired
   SET running_unit_id = NULL, next_run_at = NOW(3), updated_at = NOW(3)
 WHERE target = 'tiup-prod' AND pkg = 'tidb' AND os = 'linux' AND arch = 'amd64'
   AND running_unit_id = '<unit id from above>';
```

Settling the unit itself (through `POST /publish/units/{id}/retry`) is the
preferred route: it re-queues the failed steps and closes the desired row on
its own.

### Change the cadence or the target list

`publish.nightly.*` is hot: a new window, anchor, jitter or target list takes
effect within 15s, and only affects rows whose `next_run_at` is computed
afterwards (existing timestamps are not rewritten). See
[`config-reload.md`](./config-reload.md).

### Roll the feature back

Set `publish.nightly.enabled: false` on the API and on the workers. Nightly
pushes are published immediately again and `publish_desired` becomes inert
(rows stay, nothing claims them) — no data migration is needed to turn it back
on.

## Signals

Metrics on the worker `/metrics` endpoint:

| Metric | Use |
| --- | --- |
| `publish_desired_due_total{target}` | rows this worker claimed: the schedule is running |
| `publish_desired_lag_seconds{target}` | age of the oldest unpublished desired artifact |
| `nightly_publish_total{result}` | settled nightly units by outcome (`succeeded`, `failed`; a nightly unit has one step, so `partial` cannot occur) |
| `publish_lane_wait_seconds{target}`, `publish_lane_hold_seconds{target}`, `publish_lane_busy_total{target}` | lane contention; a rising wait with a flat hold means the lane is the bottleneck |
| `publish_lane_starvation_seconds{priority,target}` | oldest pending step per priority: proves nightly is not starving release work |

Alert: **`[tibuild] nightly publish failed`** — one aggregated message per tick
after one or more nightly units fail, at most once every 10 minutes. The body
carries `channels=<n>`, the nightly version, a bounded `target/pkg/os/arch`
sample and the first error. This is the fast signal: a mirror that refuses every
publish fails a whole window within minutes, and it is the only alert that
distinguishes "the publish was rejected" from "the publish never happened".
The row keeps its `desired_version` and the next window retries, so the alert is
informational — act on the cause, not on individual rows.

Alert: **`[tibuild] nightly publish lag`** — sent when a row's `desired_at` is
older than 1.5 windows and `last_succeeded_version` is not the desired version.
The body carries target, package, platform, desired version, lag and window.
Deduplication is one hour per row **per process**, so a multi-replica
deployment can repeat the same alert. Because the nightly version string is a
constant, a channel that succeeded once always looks published to this check;
treat `nightly publish failed` as the primary signal and this one as the
backstop for "nothing was even attempted".

When the alert fires, check in this order: `last_error` on the row → the unit's
steps (`GET /publish/units/{id}`) → lane starvation metrics → the mirror's
publish verdicts (an authorization failure looks identical for every package,
as in [Troubleshooting](#troubleshooting)).

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| every package fails with `The server refused, make sure you have access to this component` | mirror-side publisher-key authorization (rotation in progress, wrong key mounted) — not a convergence problem | the worker's `/root/.tiup/keys/private.json` mtime, the mirror operator's key allowlist |
| rows keep `running_unit_id` and never publish | a dead worker left the claim behind | `publish_unit.state`, then re-arm as above |
| `publish_desired_lag_seconds` grows while units succeed | lane congestion, or a window longer than the build cadence | `publish_lane_starvation_seconds`, `oldest_pending_seconds` of queued units |
| pushes create units instead of deferred rows | `publish.nightly` missing from the API's config, or the target not listed under `targets` | the API's loaded config and its startup log line per target |
| deferred pushes create no publication at all | worker not running, or `worker.kinds` does not include `tiup` | worker logs: `nightly target schedule` and `nightly desired-state reconciler started` |
| a row skips its windows: `next_run_at` sits on another target's boundary (an hourly staging row parked at `14:1x`) | a worker that does not shard the target advanced the row. Settles are now scoped to the worker's own targets, so this means the row was written before that scoping shipped (or a worker runs with a stale shard) | compare `next_run_at` with the row's target window, and the worker's `nightly target schedule` line; re-arm the row as above, it is safe |

## Limits

- **Latest wins.** There is no automatic rollback to the last good version; the
  policy is `on_failure: alert` for now.
- **One row per channel.** The table holds the current wish, not a history; use
  `publish_unit` / `publish_step` for what actually happened.
- **No manual ordering.** Packages due in the same window publish in
  `next_run_at` order, which is jitter order, not a priority chosen by hand. A
  release publication overtakes them through the lane priority.
