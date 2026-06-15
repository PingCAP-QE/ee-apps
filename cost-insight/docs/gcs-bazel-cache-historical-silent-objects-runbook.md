# GCS Bazel Cache Historical Silent Objects Runbook

## Purpose

This runbook executes a one-time evaluation of historical silent objects in
`pingcap-ci-bazel-remote-cache-us-central1`.

A historical silent object is defined as an object that:

- exists in the current inventory snapshot
- has `time_created < 2026-05-25 07:01:34 UTC`
- does not exist in
  `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`

In plain terms, it already existed before the GCS Data Access audit-log window
began and has had no observed `storage.objects.get` or `storage.objects.create`
activity since then.

## Inputs

### Audit-log-backed current state

```text
pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current
```

### Inventory snapshot

```text
gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/
```

Snapshot metadata:

- manifest rows: `248,734,953`
- snapshot time: `2026-06-11T00:28:58.390558Z`
- target datetime: `2026-06-11`

### One-time BigQuery tables

```text
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611
```

## Step 1: Confirm Inventory Report Completeness

Check that the manifest exists and the parquet shards are present:

```bash
gcloud storage cat \
  gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/5183fd89-65d6-4067-b511-fc91f0ddea71_2026-06-11T00:00_manifest.json

gcloud storage ls \
  gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/*_2026-06-11T00:28_*.parquet \
  | wc -l
```

Expected outcome:

- manifest shows `recordsProcessed = 248734953`
- parquet shard count should match the report's shard count

## Step 2: Load Parquet Into A Raw BigQuery Staging Table

Load the parquet shards directly into BigQuery. This step is a BigQuery load
job, so the load itself is free.

Note: for this report layout, the direct wildcard form was not accepted by
`bq load` during execution on 2026-06-14. The reliable approach is to expand
the shard list first and then pass the comma-separated URI list to `bq load`.

```bash
uris=$(
  gcloud storage ls \
    'gs://pingcap-ci-console-logs-us-central1/gcs-cache-inventory/2026-06-10/*_2026-06-11T00:28_*.parquet' \
    | paste -sd, -
)

bq --location=US load \
  --replace \
  --source_format=PARQUET \
  pingcap-testing-account:ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611 \
  "$uris"
```

The expected raw schema includes at least:

- `bucket`
- `name`
- `size`
- `timeCreated`

## Step 3: Project Into The Normalized Analysis Table

Create the one-time normalized table with only the fields needed for the
evaluation.

```bash
bq --location=US query --nouse_legacy_sql '
CREATE OR REPLACE TABLE `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611` AS
SELECT
  name AS object_name,
  timeCreated AS time_created,
  size AS size_bytes
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611`
WHERE bucket = "pingcap-ci-bazel-remote-cache-us-central1"
'
```

Normalized schema:

- `object_name STRING`
- `time_created TIMESTAMP`
- `size_bytes INT64`

## Step 4: Validate Imported Row Count

Check that the normalized table row count matches the manifest:

```bash
bq --location=US query --nouse_legacy_sql '
SELECT COUNT(*) AS object_count
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611`
'
```

Expected result:

- `248734953`

If the row count does not match, stop and investigate before running the join
analysis.

## Step 5: Run The Main Summary Query

This query produces the primary output metrics for the historical silent
population.

```bash
bq --location=US query --nouse_legacy_sql '
WITH inventory AS (
  SELECT
    object_name,
    time_created,
    size_bytes
  FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611`
),
pre_20260525 AS (
  SELECT
    object_name,
    time_created,
    size_bytes
  FROM inventory
  WHERE time_created < TIMESTAMP("2026-05-25 07:01:34 UTC")
),
historical_silent AS (
  SELECT
    inv.object_name,
    inv.time_created,
    inv.size_bytes
  FROM pre_20260525 AS inv
  LEFT JOIN `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` AS last
    USING (object_name)
  WHERE last.object_name IS NULL
),
all_inventory_totals AS (
  SELECT
    COUNT(*) AS all_inventory_object_count,
    COALESCE(SUM(size_bytes), 0) AS all_inventory_size_bytes
  FROM inventory
),
pre_window_totals AS (
  SELECT
    COUNT(*) AS pre_20260525_inventory_object_count,
    COALESCE(SUM(size_bytes), 0) AS pre_20260525_inventory_size_bytes
  FROM pre_20260525
),
historical_silent_totals AS (
  SELECT
    COUNT(*) AS historical_silent_object_count,
    COALESCE(SUM(size_bytes), 0) AS historical_silent_size_bytes
  FROM historical_silent
)
SELECT
  historical_silent_object_count,
  SAFE_DIVIDE(
    historical_silent_object_count,
    all_inventory_object_count
  ) AS historical_silent_object_ratio_vs_all_inventory,
  SAFE_DIVIDE(
    historical_silent_object_count,
    pre_20260525_inventory_object_count
  ) AS historical_silent_object_ratio_vs_pre_20260525_inventory,
  historical_silent_size_bytes,
  SAFE_DIVIDE(
    historical_silent_size_bytes,
    all_inventory_size_bytes
  ) AS historical_silent_size_ratio_vs_all_inventory_bytes,
  SAFE_DIVIDE(
    historical_silent_size_bytes,
    pre_20260525_inventory_size_bytes
  ) AS historical_silent_size_ratio_vs_pre_20260525_inventory_bytes,
  all_inventory_object_count,
  pre_20260525_inventory_object_count,
  all_inventory_size_bytes,
  pre_20260525_inventory_size_bytes
FROM historical_silent_totals
CROSS JOIN all_inventory_totals
CROSS JOIN pre_window_totals
'
```

Required outputs:

- `historical_silent_object_count`
- `historical_silent_object_ratio_vs_all_inventory`
- `historical_silent_object_ratio_vs_pre_20260525_inventory`
- `historical_silent_size_bytes`
- `historical_silent_size_ratio_vs_all_inventory_bytes`
- `historical_silent_size_ratio_vs_pre_20260525_inventory_bytes`

## Step 6: Split The Result By `ac`, `cas`, And `other`

```bash
bq --location=US query --nouse_legacy_sql '
WITH historical_silent AS (
  SELECT
    inv.object_name,
    inv.time_created,
    inv.size_bytes
  FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611` AS inv
  LEFT JOIN `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` AS last
    USING (object_name)
  WHERE inv.time_created < TIMESTAMP("2026-05-25 07:01:34 UTC")
    AND last.object_name IS NULL
)
SELECT
  CASE
    WHEN STARTS_WITH(object_name, "ac/") THEN "ac"
    WHEN STARTS_WITH(object_name, "cas/") THEN "cas"
    ELSE "other"
  END AS object_kind,
  COUNT(*) AS object_count,
  COALESCE(SUM(size_bytes), 0) AS size_bytes
FROM historical_silent
GROUP BY object_kind
ORDER BY object_kind
'
```

Validation rule:

- `ac + cas + other = total`

## Step 7: Produce Spot-Check Samples

Export a small sample list for manual review:

```bash
bq --location=US query --nouse_legacy_sql '
SELECT
  inv.object_name,
  inv.time_created,
  inv.size_bytes
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611` AS inv
LEFT JOIN `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` AS last
  USING (object_name)
WHERE inv.time_created < TIMESTAMP("2026-05-25 07:01:34 UTC")
  AND last.object_name IS NULL
ORDER BY inv.time_created, inv.object_name
LIMIT 10
'
```

Each sampled object should satisfy all of the following:

- exists in inventory
- `time_created < 2026-05-25 07:01:34 UTC`
- absent from `gcs_cache_object_last_seen_current`

## Step 8: Optional Cleanup After Analysis

If the tables are no longer needed after the delete-planning discussion, drop
them to stop storage charges.

```bash
bq --location=US rm -f -t \
  pingcap-testing-account:ci_bazel_cache_logs._tmp_gcs_cache_inventory_20260611

bq --location=US rm -f -t \
  pingcap-testing-account:ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611
```

## Validation Checklist

Do not use the output for delete planning unless all checks pass:

- normalized row count matches manifest `248734953`
- `historical_silent_object_count <= pre_20260525_inventory_object_count <= all_inventory_object_count`
- `historical_silent_size_bytes <= pre_20260525_inventory_size_bytes <= all_inventory_size_bytes`
- `ac + cas + other = total`
- at least 10 samples are manually verified

## What This Runbook Does Not Do

- It does not delete any object.
- It does not create a recurring inventory pipeline.
- It does not modify the daily `last_seen` job.
- It does not decide the final delete batching strategy.

Those are follow-up steps after the historical silent population size is known.
