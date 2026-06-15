# GCS Bazel Cache Historical Silent Object Evaluation Design

## Context

CI Bazel jobs use the GCS bucket
`pingcap-ci-bazel-remote-cache-us-central1` as a remote cache backend.

As of 2026-06-14:

- access-log source:
  `pingcap-testing-account.ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access`
- access-log window:
  `2026-05-25 07:01:34 UTC` to `2026-06-14 08:49:38 UTC`
- current `last_seen` object set:
  `80,725,633` objects in
  `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`
- inventory snapshot:
  `gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/`
- inventory manifest row count:
  `248,734,953`
- inventory snapshot time:
  `2026-06-11T00:28:58.390558Z`

The daily BigQuery summary job is already running and maintaining
`gcs_cache_object_last_seen_current` from `storage.objects.get` and
`storage.objects.create` logs. That solves the steady-state access-history
problem for the post-log window, but it still leaves a large historical blind
spot:

- objects that already existed before `2026-05-25 07:01:34 UTC`
- were never read or recreated after the log window began
- still occupy space in the bucket today

Those objects are visible in inventory, but absent from `last_seen`.

## Goal

Perform a one-time evaluation of the current bucket population to answer:

1. how many current objects were already present before
   `2026-05-25 07:01:34 UTC` and still have no post-window access record
2. what percentage of the current bucket object count they represent
3. what percentage of the current bucket bytes they represent
4. how that population splits across `ac/`, `cas/`, and `other`

This design deliberately stops at evaluation and delete preparation. It does
not define the production delete execution yet.

## Non-goals

- Adding a long-lived inventory sync pipeline.
- Adding a new CronJob or CLI command for this one-time evaluation.
- Mirroring object-level cache state into TiDB.
- Enabling delete directly from this document.
- Replacing the existing `last_seen` daily job.

## Live Findings

### What `last_seen` already covers

`gcs_cache_object_last_seen_current` is built from both
`storage.objects.get` and `storage.objects.create`.

That means an object is present in `last_seen` if it has had any observed
read or create activity during the current log window. The table is therefore
the correct post-window visibility surface for this one-time evaluation.

### The historical silent gap

For this task, the target population is:

- object is present in the current inventory snapshot
- `time_created < 2026-05-25 07:01:34 UTC`
- `object_name` does not exist in
  `gcs_cache_object_last_seen_current`

Operationally, these are "inventory-only historical objects":

- they existed before the audit-log window began
- they have no observed `get` or `create` since that moment
- they still exist in the bucket now

This is the exact population that a logs-only cleanup design cannot see.

### Measured historical silent population

The one-time evaluation was executed on 2026-06-14 against:

- inventory snapshot time: `2026-06-11T00:28:58.390558Z`
- current `last_seen` table as of the same working session

Measured result:

- `historical_silent_object_count = 178,554,492`
- `historical_silent_object_ratio_vs_all_inventory = 71.79%`
- `historical_silent_object_ratio_vs_pre_20260525_inventory = 99.30%`
- `historical_silent_size_bytes = 495,343,299,908,013`
- `historical_silent_size_ratio_vs_all_inventory_bytes = 69.78%`
- `historical_silent_size_ratio_vs_pre_20260525_inventory_bytes = 99.68%`

Split by prefix:

- `ac`: `55,790,841` objects, `29,486,820,553` bytes
- `cas`: `122,763,651` objects, `495,313,813,087,460` bytes
- `other`: `0`

This result changes the practical rollout order:

1. first handle the one-time historical silent population
2. then design recurring steady-state cleanup for post-window objects

### Delete-related operational constraints

Current live bucket state relevant to delete:

- soft delete is disabled:
  `soft_delete_policy.retentionDurationSeconds = 0`
- no lifecycle rule is present
- `storagebatchoperations.googleapis.com` was not enabled as of
  2026-06-14
- Storage batch operations additionally requires Storage Intelligence to be
  enabled for the bucket's project scope
- local `gcloud` version used for this work is `565.0.0`

Because soft delete is disabled, any object delete is permanent. That raises
the bar for the first deletion slice:

- no direct full-scale delete
- no best-effort high-concurrency custom script
- all first-pass deletion inputs must be auditable and generation-safe

## Why Keep This Evaluation In BigQuery

This one-time analysis should stay entirely in BigQuery.

Reasons:

1. the source of truth is already in BigQuery and GCS
2. object cardinality is too high for a temporary TiDB mirror to be worth it
3. the result is an offline operational analysis, not a latency-sensitive API
4. BigQuery already holds the post-window `last_seen` table we need for the
   join

## One-Time Evaluation Approach

### Input A: current `last_seen` state

Use the existing table:

```text
pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current
```

This is the authoritative view of all objects that had any observed
`storage.objects.get` or `storage.objects.create` activity during the current
log window.

### Input B: one-time inventory snapshot

Use the existing Storage Insights inventory export:

```text
gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/
```

The report is not productized into a recurring pipeline. Instead, we import it
once into a temporary BigQuery table for this evaluation and for the first
delete-preparation pass next week.

### Temporary BigQuery tables

Use two one-time tables in dataset `ci_bazel_cache_logs`:

1. raw staging table loaded from parquet
2. normalized analysis table with only the fields we actually need

Suggested names:

```text
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611
```

The raw staging table exists only because the parquet schema uses source field
names such as `name`, `size`, and `timeCreated`. The normalized table is the
one used for analysis.

Normalized table schema:

```sql
CREATE OR REPLACE TABLE `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611` AS
SELECT
  name AS object_name,
  timeCreated AS time_created,
  size AS size_bytes
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611`
WHERE bucket = "pingcap-ci-bazel-remote-cache-us-central1";
```

Only these fields are retained:

- `object_name STRING`
- `time_created TIMESTAMP`
- `size_bytes INT64`

These fields are intentionally not retained in the normalized table:

- `snapshot_date`
- `snapshot_time`
- `storage_class`
- `object_kind`
- `generation`

`snapshot_time` still matters, but only as run metadata in the runbook and
operator notes. It does not need to be stored per row.

## Core Analysis Definition

The historical silent object population is defined as:

1. object exists in the normalized inventory table
2. `time_created < TIMESTAMP("2026-05-25 07:01:34 UTC")`
3. `LEFT JOIN` to `gcs_cache_object_last_seen_current` by `object_name`
4. `WHERE last_seen.object_name IS NULL`

That yields the exact one-time cohort we want:

- existed before the log window
- still exists today
- no observed `storage.objects.get` or `storage.objects.create` in the current
  log window

`ac/`, `cas/`, and `other` are not stored in the temporary table. They are
derived at query time from `object_name`:

```sql
CASE
  WHEN STARTS_WITH(object_name, "ac/") THEN "ac"
  WHEN STARTS_WITH(object_name, "cas/") THEN "cas"
  ELSE "other"
END
```

## Output Metrics

The one-time evaluation must produce these metrics:

### Object-count metrics

- `historical_silent_object_count`
- `historical_silent_object_ratio_vs_all_inventory`
- `historical_silent_object_ratio_vs_pre_20260525_inventory`

### Byte metrics

- `historical_silent_size_bytes`
- `historical_silent_size_ratio_vs_all_inventory_bytes`
- `historical_silent_size_ratio_vs_pre_20260525_inventory_bytes`

### Prefix split

Each of the above should also be split by:

- `ac`
- `cas`
- `other`

### Spot-check sample

The evaluation should also produce a small object-name sample for manual
verification before any delete design is finalized.

## Cost Estimate

This section is intentionally approximate and is based on the current report
size and current BigQuery pricing as checked on 2026-06-14.

Official pricing references:

- query: `US $6.25 / TiB scanned`
- first `1 TiB / month` of on-demand query usage: free
- load data: free
- active logical storage: about `US $0.02 / GiB-month`

References:

- [BigQuery pricing](https://cloud.google.com/bigquery/pricing)
- [External tables pricing](https://docs.cloud.google.com/bigquery/docs/external-tables)
- [Estimate query costs](https://docs.cloud.google.com/bigquery/docs/best-practices-costs)
- [BigQuery data type sizes](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/data-types)

Inventory-report scale used for the estimate:

- parquet files total size: about `25.3 GB`
- manifest rows: `248,734,953`
- observed average `object_name` length in current `last_seen` table:
  about `67.7` bytes

Approximate cost envelope for the one-time workflow:

- load parquet into raw staging table: `US $0`
- normalized inventory table logical size: about `19.8 GiB`
- temporary-table storage:
  - about `US $0.013 / day`
  - about `US $0.09 / week`
  - about `US $0.40 / month`
- one query that scans only the normalized inventory table:
  about `US $0.12`
- one query that scans normalized inventory plus
  `gcs_cache_object_last_seen_current` for the join:
  about `US $0.15`

Important notes:

- if the billing account has not exhausted the monthly free `1 TiB` on-demand
  query allowance, the actual billed query cost can be lower or zero
- these are logical-byte estimates, not billing-export-confirmed charges
- the `US $0` statement applies to the parquet `load` step itself; the later
  SQL projection and join analysis are normal BigQuery queries

## Validation

Before using the result for delete planning, all of the following checks should
pass:

1. normalized inventory row count matches manifest `recordsProcessed`
2. `historical_silent_object_count <= pre_20260525_inventory_object_count <= all_inventory_object_count`
3. `historical_silent_size_bytes <= pre_20260525_inventory_size_bytes <= all_inventory_size_bytes`
4. `ac + cas + other = total`
5. at least 10 sample objects are manually checked to confirm:
   - they exist in inventory
   - `time_created` is earlier than `2026-05-25 07:01:34 UTC`
   - they do not exist in `gcs_cache_object_last_seen_current`

## Delete Execution Design

This section defines the first production delete design for the historical
silent population only.

It intentionally does not merge that work with the later recurring LRU cleanup
for post-window objects.

### Why not use a custom delete script first

For this first slice, a custom script that issues individual delete requests is
not the preferred path.

Reasons:

1. the target set is extremely large: `178,554,492` objects
2. the bucket has no soft delete protection
3. the initial object-write guideline for a flat bucket is on the order of
   `~1000` write or delete requests per second, and Google recommends gradual
   ramp-up
4. Cloud Storage now provides a managed bulk-delete path specifically for
   millions or billions of objects

Preferred path:

- use Cloud Storage Storage batch operations with manifest files
- include object `generation` in every manifest row
- enable Cloud Logging for both `succeeded` and `failed` transforms

### Candidate materialization

The temporary normalized inventory table is sufficient for evaluation, but not
for safe delete execution because it dropped `generation`.

Delete execution should therefore build a one-time candidate table from the raw
staging inventory table plus `gcs_cache_object_last_seen_current`.

Suggested one-time table:

```text
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614
```

Suggested schema:

- `bucket STRING`
- `object_name STRING`
- `generation INT64`
- `object_kind STRING`
- `time_created TIMESTAMP`
- `size_bytes INT64`
- `shard_id INT64`
- `candidate_reason STRING`
- `candidate_generated_at TIMESTAMP`

`shard_id` should be deterministic:

```sql
MOD(ABS(FARM_FINGERPRINT(object_name)), 256)
```

`candidate_reason` for this slice is fixed to:

```text
historical_silent_pre_20260525_no_post_window_activity
```

### Why generation is required

The manifest format used by Storage batch operations allows an optional
`generation` column.

This must be included for the first slice.

Reason:

- if a manifest contains only `bucket` and `name`, Cloud Storage deletes the
  current live object for that name
- some objects may have been recreated after the inventory snapshot
- using `generation` pins deletion to the exact snapshot generation and avoids
  deleting a newer live object that reused the same name

### Delete phases

The delete rollout should be split into four phases.

#### Phase 0: final freeze and refresh

Before any delete job is created:

1. refresh `gcs_cache_object_last_seen_current` through the latest complete UTC
   day
2. rebuild the delete candidate table from the inventory snapshot plus the
   refreshed `last_seen`
3. export immutable manifests from that candidate table
4. do not modify the candidate table after manifest export

This phase exists to make the delete input auditable and reproducible.

#### Phase 1: `ac` canary

Create one small canary manifest for `ac` only.

Suggested size:

- `10,000` objects

Suggested ordering:

- oldest `time_created` first

After the canary batch job completes:

- wait `12-24` hours
- inspect Cloud Logging job results
- inspect CI behavior for unexpected cache-miss amplification

#### Phase 2: remaining `ac`

If the canary is clean:

- delete the remaining `ac` historical silent objects

This is still meaningful as a safety gate even though `ac` bytes are tiny,
because it validates:

- manifest generation handling
- batch-operation observability
- operator workflow

#### Phase 3: `cas` main cleanup

Delete `cas` candidates in shard groups.

Suggested shard plan:

- total shards: `256`
- one batch job per `8` shards
- initial concurrency: `1` batch job
- if the first jobs are healthy, raise to `2-4` concurrent jobs

This keeps rollout pressure deliberate without inventing custom client-side
rate control.

#### Phase 4: post-delete re-measurement

After the historical silent sweep completes:

1. generate a fresh inventory report
2. rerun the one-time evaluation query shape
3. confirm that the historical silent population collapsed as expected
4. only then move on to recurring steady-state LRU delete design

### Storage batch operations requirements

Prerequisites for the first delete slice:

1. enable `storagebatchoperations.googleapis.com`
2. enable Storage Intelligence for the project scope that contains the bucket
   and make an explicit edition decision
3. use a principal with `roles/storage.admin` on the project or bucket
4. store manifests in a Cloud Storage bucket path that the operator can read
5. enable job logging with:
   - `--log-actions=transform`
   - `--log-action-states=succeeded,failed`

Because the bucket already has Storage Insights inventory configured, the
remaining missing prerequisite is Storage Intelligence enrollment for batch
operations.

### Manifest strategy

Manifests should be created as CSV files with header:

```text
bucket,name,generation
```

Each manifest should contain objects from only one source bucket:

```text
pingcap-ci-bazel-remote-cache-us-central1
```

Suggested manifest layout:

```text
gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/
  ac-canary.csv
  ac-rest.csv
  cas-shards-000-007.csv
  cas-shards-008-015.csv
  ...
```

### Stop conditions

The delete rollout should stop immediately if any of the following occurs:

1. unexpected failed transforms exceed `0.1%`
2. unexpected failed transforms exceed `1000` objects for a job
3. CI cache behavior shows clear regression after the `ac` canary
4. operator validation reveals manifest-generation mismatch or candidate drift

Expected non-fatal outcomes:

- `not found`
- generation mismatch against newer objects

These should be recorded and reviewed, but they are not by themselves a reason
to treat the whole job as unsafe.

### Separation from future steady-state cleanup

This delete design is only for the historical silent population identified by
the inventory snapshot.

The later recurring cleanup design should remain separate:

- input: post-window `last_seen` candidates
- cadence: weekly
- retention: still expected to start from `ac=28d`, `cas=42d`
- implementation home: existing `cost-insight` plus `ee-ops` CronJob path

Do not combine the first historical cleanup with that steady-state workflow in
the same initial rollout.

## Relationship To The Existing `last_seen` Pipeline

The current daily job remains unchanged:

- `sync-gcs-cache-last-seen` continues to summarize one UTC day of access logs
- `gcs_cache_object_last_seen_current` continues to be the steady-state
  post-window access table

This one-time inventory-based analysis is additive. It closes the pre-log blind
spot without introducing a new recurring pipeline yet.

## Next Step After This Evaluation

After the historical silent population is quantified, the next delete design
discussion can decide:

1. the exact manifest export queries and batch-job commands
2. the canary timing and approval checkpoint
3. the shard grouping for `cas`
4. whether the ongoing steady-state cleanup should continue to rely on
   `last_seen` plus future inventory refreshes

That delete execution design is intentionally deferred until the one-time
evaluation result is in hand.
