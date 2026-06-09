# GCS Bazel Cache Cleanup Design

## Context

CI Bazel jobs use the GCS bucket
`pingcap-ci-bazel-remote-cache-us-central1` as a remote cache backend.

As of 2026-06-09, this bucket is the dominant storage consumer in the
`pingcap-testing-account` project:

- bucket size: about `589.97 TiB`
- lifecycle: none
- access log source: `pingcap-testing-account.ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access`
- log sink: `ci-bazel-cache-gcs-data-access-to-bq`

The bucket is already exporting Cloud Storage Data Access audit logs into
BigQuery. The next step is to turn that access history into a safe cleanup
workflow based on `last_seen_at`, not object age.

One important caveat is that the access log was enabled recently, not at
bucket inception. This bucket already contains a large historical object set,
and many older objects may have produced neither `storage.objects.create` nor
`storage.objects.get` events during the current audit-log window.

This design keeps the implementation in `cost-insight`, because the project
already owns:

- BigQuery-backed cost and usage jobs
- recurring batch jobs packaged into `cost-insight-jobs`
- the `ee-apps` to `ee-ops` CronJob rollout flow

## Goals

1. Compute object-level `last_seen_at` for the Bazel remote cache with low
   recurring BigQuery cost.
2. Support a weekly dry-run and delete workflow based on "N days without
   access".
3. Keep the logic versioned in `ee-apps`, with deployment through `ee-ops`
   CronJobs.
4. Make the first slice conservative enough to observe safely before enabling
   deletes.
5. Cover the full current bucket object set before enabling delete, including
   historical objects that predate the audit-log window.

## Non-goals

- Adding a dashboard page in the first slice.
- Building a general GCS janitor for arbitrary buckets.
- Exact deleted-byte accounting in the first slice.
- Replacing GCS lifecycle rules for buckets whose retention can be expressed by
  object age alone.
- Enabling production delete from a logs-only view of the bucket.

## Live Findings

### Access window maturity

As of 2026-06-09, the access-log table covers:

- `first_ts = 2026-05-25 07:01:34 UTC`
- `last_ts = 2026-06-09 06:35:08 UTC`

That is enough to evaluate short-term reuse and to start a dry-run workflow,
but not enough to claim that a `28-day no access` deletion policy is already
validated by a full 28-day window. The earliest date when a full 28-day policy
can be judged from this log stream is approximately 2026-06-22.

### Read activity shape

Recent log shape from the live table:

- total rows in current window: about `659.7M`
- `storage.objects.get`: about `575.0M`
- `storage.objects.create`: about `84.7M`
- one sampled day (`2026-06-08`) had about `39.6M` gets and about `7.24M`
  creates

### Reuse sample

A deterministic `0.1%` sample of objects created between 2026-05-26 and
2026-06-01 was checked against subsequent reads through 2026-06-09.

Key findings:

- sampled cohort size: `23,199` objects
- `75.1%` were never read again after create
- only `0.35%` were still read on or after day 7
- only `0.14%` were still read on or after day 10
- only `0.02%` were still read on or after day 14

Split by kind:

- `ac/` cools slightly faster than `cas/`
- both prefixes become very cold after the first few days

This supports a future LRU cleanup policy, but the first delete threshold
should still be conservative because the available observation window is short.

### Historical object gap

Objects that were created before `2026-05-25` and have not produced any
`storage.objects.get` or `storage.objects.create` event since that date will
not be visible to the first-slice summary tables. This is not a small edge
case. Because the access log was enabled after the bucket had already been in
use, there may be a large historical population of still-existing objects that
are completely invisible to the log-derived tables.

That means:

1. the current `last_seen` tables are accurate only for the log-visible subset
   of objects
2. a logs-only dry-run can help validate query shape and recent reuse behavior,
   but it is not a complete candidate view for the whole bucket
3. production delete must not rely only on the log-derived tables

The design correction is to add a full object inventory source before delete.
The recommended approach is GCS Inventory or Storage Insights exported into
BigQuery, then joined with the log-derived `last_seen` state.

After inventory is added, historical silent objects can still participate in a
conservative LRU policy:

- if an object existed before `2026-05-25` and no read has been observed since
  `2026-05-25 07:01:34 UTC`, then by `2026-06-22 07:01:34 UTC` it is provably
  at least 28 days cold
- the same logic makes `42-day` silence provable on
  `2026-07-06 07:01:34 UTC`

So the real missing piece is not only `last_seen_at`, but full object
enumeration.

## Why Keep Object State in BigQuery

The canonical object-access state should live in BigQuery, not TiDB.

Reasons:

1. The source of truth already lives in BigQuery.
2. Object cardinality is high enough that daily object-level upserts would make
   TiDB a poor fit for the first slice.
3. The primary consumer is the cleanup job, not a latency-sensitive product
   API.
4. Keeping object state close to the source avoids an extra BigQuery -> app ->
   TiDB transport step for millions of objects per day.

TiDB may still be useful later for small control-plane or reporting tables, but
the first slice should not mirror object-level cache state into TiDB.

## Proposed Architecture

```text
GCS Data Access logs
  -> BigQuery raw table
  -> cost-insight daily summary job
  -> BigQuery object_last_seen tables

GCS Inventory / Storage Insights
  -> BigQuery inventory snapshot table

object_last_seen + inventory snapshot
  -> cleanup state / candidate query
  -> cost-insight weekly cleanup job
  -> GCS delete operations
```

Implementation ownership:

- `ee-apps/cost-insight`
  - Python job code
  - SQL templates
  - design docs
- `ee-ops/apps/gcp/cost-insight`
  - CronJob manifests
  - service account wiring
  - rollout sequencing

## BigQuery Data Model

Use the existing dataset `ci_bazel_cache_logs` in project
`pingcap-testing-account`, because it is already in the same region as the raw
audit-log table.

### `gcs_cache_object_last_seen_daily`

Daily object-level aggregation from both `storage.objects.get` and
`storage.objects.create`.

Suggested schema:

```sql
CREATE TABLE IF NOT EXISTS `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_daily` (
  ds DATE NOT NULL,
  object_name STRING NOT NULL,
  object_kind STRING NOT NULL,
  first_seen_at TIMESTAMP NOT NULL,
  last_seen_at TIMESTAMP NOT NULL,
  get_count_in_day INT64 NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
PARTITION BY ds
CLUSTER BY object_kind, object_name;
```

Column meanings:

- `ds`: logical source day from `DATE(timestamp)`
- `object_name`: extracted from
  `protopayload_auditlog.resourceName`
- `object_kind`: `cas`, `ac`, or `other`
- `first_seen_at`: earliest event timestamp for that object in that day
- `last_seen_at`: latest create-or-read timestamp for that object in that day
- `get_count_in_day`: daily read count for that object
- `updated_at`: job write timestamp

### `gcs_cache_object_last_seen_current`

Current object state, one row per object.

Suggested schema:

```sql
CREATE TABLE IF NOT EXISTS `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` (
  object_name STRING NOT NULL,
  object_kind STRING NOT NULL,
  first_seen_at TIMESTAMP,
  last_seen_at TIMESTAMP NOT NULL,
  last_seen_date DATE NOT NULL,
  total_get_count INT64 NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
CLUSTER BY object_kind, last_seen_date;
```

This table is one input to the weekly cleanup job, but it is not sufficient by
itself for full-bucket cleanup decisions.

By itself, this table is not a full-bucket inventory. It only tracks objects
that are visible in the current audit-log window.

### Required second-source table: `gcs_cache_object_inventory_current`

Current object inventory, one row per object currently present in the bucket.

Suggested schema:

```sql
CREATE TABLE IF NOT EXISTS `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_inventory_current` (
  snapshot_date DATE NOT NULL,
  object_name STRING NOT NULL,
  object_kind STRING NOT NULL,
  time_created TIMESTAMP,
  size_bytes INT64,
  updated_at TIMESTAMP NOT NULL
)
PARTITION BY snapshot_date
CLUSTER BY object_kind, object_name;
```

Expected source:

- preferred: GCS Inventory or Storage Insights export into BigQuery
- fallback: a one-time bootstrap listing job plus periodic refresh, if the
  platform path for inventory is blocked

Purpose:

- enumerate the whole current bucket object set
- provide size metadata for better dry-run reporting
- expose historical silent objects that do not appear in audit logs

### Derived cleanup view: `gcs_cache_object_cleanup_state_current`

This can be a table or a query-level CTE. It joins current inventory with
log-derived `last_seen` state.

Suggested fields:

- `object_name`
- `object_kind`
- `time_created`
- `size_bytes`
- `log_first_seen_at`
- `log_last_seen_at`
- `has_log_activity`
- `is_pre_window_inventory_only`
- `no_access_observed_since`

Semantics:

- for log-visible objects, `log_last_seen_at` is the cleanup decision input
- for inventory-only historical objects, `log_last_seen_at` is unknown, but
  `no_access_observed_since = 2026-05-25 07:01:34 UTC` is still provable if the
  object exists in inventory and has no post-window log activity
- delete eligibility for inventory-only historical objects must be based on
  provable silence since log start, not guessed last access time

### Optional future table: `gcs_cache_cleanup_run_reports`

The first slice can log run summaries to stdout and Cloud Logging only.
If later we want durable run history in BigQuery, add a small report table such
as:

- `run_ts`
- `mode`
- `ac_retention_days`
- `cas_retention_days`
- `candidate_object_count`
- `deleted_object_count`
- `error_count`

No object-level candidate archive is required in the first slice.

## Job Design

### 1. Daily summary job

CLI shape:

```bash
cost-insight sync-gcs-cache-last-seen --run-date 2026-06-08
```

Responsibilities:

1. Read one source day from
   `ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access`
2. Filter to:
   - `resource.labels.bucket_name = "pingcap-ci-bazel-remote-cache-us-central1"`
   - `protopayload_auditlog.methodName IN ("storage.objects.get", "storage.objects.create")`
3. Extract `object_name`
4. Classify `object_kind`
5. Seed newly created objects into the summary even if they were never read
6. Aggregate into `gcs_cache_object_last_seen_daily`
7. `MERGE` daily results into `gcs_cache_object_last_seen_current`
8. Emit a JSON summary with:
   - source rows scanned
   - distinct objects touched
   - BigQuery bytes processed
   - run date

Important behavior:

- rerunnable for the same `run-date`
- one source day per run
- no GCS delete behavior
- not a complete bucket inventory on its own

Suggested merge behavior:

- `first_seen_at`: keep the earliest known timestamp
- `last_seen_at`: keep the greatest value
- `total_get_count`: add only `storage.objects.get` counts
- `updated_at`: set to current run time

### 2. Weekly cleanup job

CLI shape:

```bash
cost-insight cleanup-gcs-cache --mode dry-run
cost-insight cleanup-gcs-cache --mode delete
```

Responsibilities:

1. Read the inventory-backed cleanup state
2. Join with `gcs_cache_object_inventory_current` so the job sees the full
   current bucket object set
3. Build candidates with separate retention windows for `ac` and `cas`
4. Exclude `other`
5. In `dry-run` mode:
   - print summary
   - split counts into `log-visible` and `inventory-only historical`
   - print example candidate samples
   - exit without deleting
6. In `delete` mode:
   - delete candidates in batches
   - collect success and error counts
   - stop if safety thresholds are exceeded

Suggested first-slice flags:

```text
--mode dry-run|delete
--ac-retention-days 28
--cas-retention-days 42
--max-delete-objects 50000
--batch-size 1000
--sample-limit 100
```

Deletion should use the Python GCS client rather than shelling out to
`gcloud storage rm`, so retries and per-object error handling stay in one
process.

## Query Shapes

### Daily summary query

Core aggregation shape:

```sql
SELECT
  DATE(timestamp) AS ds,
  REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$") AS object_name,
  CASE
    WHEN STARTS_WITH(REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$"), "cas/") THEN "cas"
    WHEN STARTS_WITH(REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$"), "ac/") THEN "ac"
    ELSE "other"
  END AS object_kind,
  MIN(timestamp) AS first_seen_at,
  MAX(timestamp) AS last_seen_at,
  COUNTIF(protopayload_auditlog.methodName = "storage.objects.get") AS get_count_in_day,
  CURRENT_TIMESTAMP() AS updated_at
FROM `pingcap-testing-account.ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access`
WHERE DATE(timestamp) = @run_date
  AND resource.labels.bucket_name = "pingcap-ci-bazel-remote-cache-us-central1"
  AND protopayload_auditlog.methodName IN ("storage.objects.get", "storage.objects.create")
GROUP BY ds, object_name, object_kind;
```

Measured dry-run cost for one daily source partition:

- bytes processed: `9,885,639,778` bytes
- about `9.89 GB`

### Weekly cleanup query

The weekly cleanup job should not read the raw audit-log table. It should read
the inventory-backed cleanup state built from
`gcs_cache_object_last_seen_current` plus
`gcs_cache_object_inventory_current`.

Example candidate query:

```sql
SELECT object_name, object_kind, size_bytes, effective_last_seen_at
FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_cleanup_state_current`
WHERE (
    object_kind = 'ac'
    AND (
      (
        has_log_activity
        AND effective_last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_retention_days DAY)
      ) OR (
        is_pre_window_inventory_only
        AND no_access_observed_since <= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_retention_days DAY)
      )
    )
  ) OR (
    object_kind = 'cas'
    AND (
      (
        has_log_activity
        AND effective_last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_retention_days DAY)
      ) OR (
        is_pre_window_inventory_only
        AND no_access_observed_since <= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_retention_days DAY)
      )
    )
  )
ORDER BY object_kind, effective_last_seen_at
LIMIT @max_delete_objects;
```

## Cost Model

BigQuery scheduled queries and manual queries are priced the same. The first
slice will run from a CronJob in `ee-ops`, but the underlying pricing model is
still regular BigQuery query processing.

At the current documented on-demand US rate:

- first `1 TiB` per month per billing account: free
- above that: `US $6.25 / TiB`

Reference:

- [BigQuery pricing](https://cloud.google.com/bigquery/pricing?hl=en_US)
- [Scheduling queries](https://docs.cloud.google.com/bigquery/docs/scheduling-queries)

### Estimated recurring scan cost

Measured daily summary query:

- about `9.89 GB/day`
- about `69.2 GB/week`
- about `0.0629 TiB/week`
- about `US $0.39/week` at on-demand pricing before free-tier effects

This means the recurring summary pipeline is cheap enough to run daily.

### Why daily instead of weekly summary

Daily summary is not chosen because it is dramatically cheaper than an ideal
weekly incremental summary. If both scan each source day exactly once, total
scan volume is similar.

Daily summary is chosen because it gives:

1. smaller failure domains
2. simpler reruns
3. a stable object-state table for weekly cleanup
4. no need for the weekly cleaner to rescan raw access logs

### Ad hoc analysis cost

Measured sample query cost for a 14-day cohort study:

- bytes processed: `128,657,528,203`
- about `128.7 GB`
- about `US $0.73/run`

A similar 28-day access-pattern study would be expected to cost on the order of
`~US $1.46/run`, assuming roughly linear growth in scanned days.

This is acceptable for one-off validation, but it is not the right shape for a
recurring weekly cleanup decision path.

## What the First Slice Will Not Measure Exactly

The current access-log table is strong on access recency, but weak on object
size and whole-bucket coverage:

- `storage.objects.get` rows often contain `metadataJson.requested_bytes`
- `storage.objects.create` rows do not reliably expose full object size
- historical silent objects may have no log rows in the current window

Therefore, the first slice should report:

- candidate object count
- candidate split by `ac` and `cas`
- oldest and newest candidate `last_seen_at`
- example candidate names

The first slice should not promise exact reclaimable TiB per run.

If exact deleted-byte accounting and full-bucket coverage are required, add a
second source such as GCS Inventory or Storage Insights and join it by
`object_name`.

## Recommended Retention Policy

### Dry-run phase

Start immediately with weekly dry-runs:

- `ac`: `28 days no access`
- `cas`: `42 days no access`

Reasoning:

- the sample shows very fast cooling
- `cas` is the dominant storage consumer, but it is safer to retain longer in
  the first delete slice
- `ac` can be more aggressive because cache misses on action metadata are less
  risky than a broken reference chain to missing content blobs

### Delete phase

Do not enable recurring delete until both conditions are true:

1. the access-log window has matured far enough for the chosen retention
2. inventory has been added so historical silent objects are not invisible

Earliest possible dates after the current log start are:

- `ac = 28 days`: approximately `2026-06-22 07:01:34 UTC`
- `cas = 42 days`: approximately `2026-07-06 07:01:34 UTC`

These dates are necessary but not sufficient. Delete should still remain
blocked until the inventory join is in place.

Even after that date, the first production step should be:

1. keep the delete CronJob present but suspended
2. run dry-run for at least 2-3 consecutive weeks
3. inspect candidate volume and job health
4. unsuspend delete with conservative per-run limits

## Safety Guards

The weekly delete job should include the following guards:

1. `dry-run` as the default mode
2. separate retention windows for `ac` and `cas`
3. exclude `other`
4. `concurrencyPolicy: Forbid`
5. `suspend: true` for delete CronJob at initial rollout
6. per-run maximum object limit
7. batch delete limit
8. stop the run if error rate exceeds a threshold
9. idempotent handling for already-missing objects
10. inventory snapshot freshness check before any delete run

Suggested first values:

- `max-delete-objects = 50,000`
- `batch-size = 1,000`
- stop if delete errors exceed `1%` or a fixed small absolute threshold

Because exact object bytes are not available in the first slice, a strict
`max-delete-bytes` guard should be deferred until inventory is added.

## Rollout Shape

### Code placement

Add new code under:

```text
cost-insight/
  docs/gcs-bazel-cache-cleanup-design.md
  src/cost_insight/jobs/sync_gcs_cache_last_seen.py
  src/cost_insight/jobs/cleanup_gcs_cache.py
```

CLI additions in:

```text
cost-insight/src/cost_insight/jobs/cli.py
```

### CronJob placement

Add new manifests beside the existing cost-insight CronJobs in:

```text
ee-ops/apps/gcp/cost-insight/cronjobs.yaml
```

Suggested CronJobs:

1. `cost-insight-sync-gcs-cache-last-seen`
   - daily
   - BQ read + BQ merge only
2. `cost-insight-sync-gcs-cache-inventory`
   - daily or weekly, depending on inventory export frequency
   - refresh inventory-backed current table
3. `cost-insight-cleanup-gcs-cache-dry-run`
   - weekly
   - full-bucket dry-run only
4. `cost-insight-cleanup-gcs-cache-delete`
   - weekly
   - initially `suspend: true`

The same `cost-insight-jobs` image can own all three commands.

### IAM and runtime

The CronJob service account needs:

- BigQuery job execution
- read/write access to dataset `ci_bazel_cache_logs`
- GCS object delete permission on
  `pingcap-ci-bazel-remote-cache-us-central1`

If the existing `ci-dashboard` workload identity path is reused, add only the
minimum incremental GCS permission needed for delete.

## Concurrency and Recovery

Only one active instance of each cleanup job type should run at a time.

Recommended rules:

- summary job may rerun safely for the same day
- inventory sync may rerun safely for the same snapshot day
- dry-run and delete must never overlap
- a failed delete run should be rerunnable without manual cleanup of partially
  processed object lists

If later we need stronger operator visibility, add a small run-state table in
TiDB or a BigQuery run-report table. That is not required for the first slice.

## Open Questions

1. Do we want to use GCS Inventory or Storage Insights as the full object
   inventory source?
2. If inventory export is not immediately available, do we want a one-time
   bootstrap listing job as an interim fallback?
3. Should the first delete slice remove only `ac`, or is `ac=28d` and
   `cas=42d` acceptable immediately after the dry-run period?
4. Do we want a small API or SQL report later for cleanup history, or are
   CronJob logs enough?
