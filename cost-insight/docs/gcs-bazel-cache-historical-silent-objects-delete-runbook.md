# GCS Bazel Cache Historical Silent Objects Delete Runbook

## Purpose

This runbook defines the first production delete workflow for the historical
silent population in `pingcap-ci-bazel-remote-cache-us-central1`.

This runbook only covers the one-time historical silent sweep. It does not
cover the later recurring steady-state LRU cleanup.

## Safety Summary

Current live constraints as of 2026-06-14:

- bucket soft delete is disabled
- bucket lifecycle is not configured
- delete is therefore permanent
- historical silent candidates are extremely large in count and bytes

Because of that, this runbook uses:

- immutable BigQuery candidate tables
- manifest files that include `generation`
- Cloud Storage Storage batch operations
- a staged rollout: `ac` canary, `ac` full, then `cas` shard groups

## Prerequisites

### Required APIs

Enable the batch-operations API:

```bash
gcloud services enable storagebatchoperations.googleapis.com \
  --project=pingcap-testing-account
```

### Required Storage Intelligence enrollment

Storage batch operations is not available until Storage Intelligence is enabled
for the bucket's project scope.

CLI entrypoint:

```bash
gcloud storage intelligence-configs enable \
  --project=pingcap-testing-account \
  --trial-edition
```

Important note:

- the `TRIAL` tier enables batch operations immediately
- according to the current product documentation, the 30-day trial
  automatically upgrades to `STANDARD` if it is not disabled before the trial
  ends

Do not run this command casually. Treat it as a billing-affecting change that
should be explicitly approved by the bucket owner.

### Required permissions

The operator should have:

- `roles/storage.admin`
- enough permission to read and write objects in
  `gs://pingcap-ci-console-logs-us-central1/`
- enough permission to run BigQuery queries and export results in
  `pingcap-testing-account.ci_bazel_cache_logs`

### Local tooling

Validated during this work:

- `gcloud` version: `565.0.0`

## Inputs

### Raw staging inventory table

```text
pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611
```

This table must exist and must contain at least:

- `bucket`
- `name`
- `size`
- `timeCreated`
- `generation`

### Current `last_seen` table

```text
pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current
```

## Step 1: Build The Immutable Delete Candidate Table

Create the delete candidate table directly from raw inventory plus current
`last_seen`.

```bash
bq --location=US query --nouse_legacy_sql '
CREATE OR REPLACE TABLE `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614` AS
SELECT
  raw.bucket AS bucket,
  raw.name AS object_name,
  raw.generation AS generation,
  CASE
    WHEN STARTS_WITH(raw.name, "ac/") THEN "ac"
    WHEN STARTS_WITH(raw.name, "cas/") THEN "cas"
    ELSE "other"
  END AS object_kind,
  raw.timeCreated AS time_created,
  raw.size AS size_bytes,
  MOD(ABS(FARM_FINGERPRINT(raw.name)), 256) AS shard_id,
  "historical_silent_pre_20260525_no_post_window_activity" AS candidate_reason,
  CURRENT_TIMESTAMP() AS candidate_generated_at
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_inventory_raw_20260611` AS raw
LEFT JOIN `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` AS last
  ON raw.name = last.object_name
WHERE raw.bucket = "pingcap-ci-bazel-remote-cache-us-central1"
  AND raw.timeCreated < TIMESTAMP("2026-05-25 07:01:34 UTC")
  AND last.object_name IS NULL
'
```

## Step 2: Validate Candidate Totals

Verify the candidate totals before any manifest export:

```bash
bq --location=US query --nouse_legacy_sql '
SELECT
  object_kind,
  COUNT(*) AS object_count,
  COALESCE(SUM(size_bytes), 0) AS size_bytes
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614`
GROUP BY object_kind
ORDER BY object_kind
'
```

Expected baseline from the current analysis:

- `ac`: `55,790,841` objects
- `cas`: `122,763,651` objects
- `other`: `0`

## Step 3: Export The `ac` Canary Manifest

Export a single small canary manifest with header `bucket,name,generation`.

```bash
bq --location=US query --nouse_legacy_sql '
EXPORT DATA OPTIONS (
  uri = "gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/ac-canary.csv",
  format = "CSV",
  overwrite = true,
  header = true
) AS
SELECT
  bucket,
  object_name AS name,
  generation
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614`
WHERE object_kind = "ac"
ORDER BY time_created, object_name
LIMIT 10000
'
```

## Step 4: Dry-Run The `ac` Canary Batch Job

Create a dry-run batch job first.

```bash
gcloud storage batch-operations jobs create gcs-cache-ac-canary-dry-run-20260614 \
  --bucket=gs://pingcap-ci-bazel-remote-cache-us-central1 \
  --manifest-location=gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/ac-canary.csv \
  --delete-object \
  --dry-run \
  --log-actions=transform \
  --log-action-states=succeeded,failed \
  --description='Dry run for historical silent AC canary delete'
```

Confirm the dry-run object count matches the manifest expectation.

If the command returns a permission error stating that Storage Intelligence is
not enabled for the bucket's project, stop here and complete the prerequisite
in the previous section first.

## Step 5: Execute The `ac` Canary

If the dry run is correct, create the real canary job:

```bash
gcloud storage batch-operations jobs create gcs-cache-ac-canary-20260614 \
  --bucket=gs://pingcap-ci-bazel-remote-cache-us-central1 \
  --manifest-location=gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/ac-canary.csv \
  --delete-object \
  --log-actions=transform \
  --log-action-states=succeeded,failed \
  --description='Historical silent AC canary delete'
```

After completion:

- wait `12-24` hours
- inspect batch-operation logs
- inspect CI behavior for unexpected cache regression

## Step 6: Export And Delete Remaining `ac`

If the canary is clean, export the remaining `ac` manifest.

```bash
bq --location=US query --nouse_legacy_sql '
EXPORT DATA OPTIONS (
  uri = "gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/ac-rest.csv",
  format = "CSV",
  overwrite = true,
  header = true
) AS
SELECT
  bucket,
  object_name AS name,
  generation
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614`
WHERE object_kind = "ac"
QUALIFY ROW_NUMBER() OVER (ORDER BY time_created, object_name) > 10000
'
```

Then create the delete job:

```bash
gcloud storage batch-operations jobs create gcs-cache-ac-rest-20260614 \
  --bucket=gs://pingcap-ci-bazel-remote-cache-us-central1 \
  --manifest-location=gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/ac-rest.csv \
  --delete-object \
  --log-actions=transform \
  --log-action-states=succeeded,failed \
  --description='Historical silent AC full delete after canary'
```

## Step 7: Export `cas` Manifests By Shard Group

Export one manifest per eight shards.

Example for shards `0-7`:

```bash
bq --location=US query --nouse_legacy_sql '
EXPORT DATA OPTIONS (
  uri = "gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/cas-shards-000-007.csv",
  format = "CSV",
  overwrite = true,
  header = true
) AS
SELECT
  bucket,
  object_name AS name,
  generation
FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614`
WHERE object_kind = "cas"
  AND shard_id BETWEEN 0 AND 7
ORDER BY shard_id, object_name
'
```

Repeat for:

- `008-015`
- `016-023`
- ...
- `248-255`

## Step 8: Delete `cas` In Controlled Concurrency

Start with a single batch job:

```bash
gcloud storage batch-operations jobs create gcs-cache-cas-shards-000-007-20260614 \
  --bucket=gs://pingcap-ci-bazel-remote-cache-us-central1 \
  --manifest-location=gs://pingcap-ci-console-logs-us-central1/gcs-cache-delete-manifests/2026-06-14/cas-shards-000-007.csv \
  --delete-object \
  --log-actions=transform \
  --log-action-states=succeeded,failed \
  --description='Historical silent CAS delete shards 000-007'
```

Recommended rollout:

1. run `1` `cas` job
2. inspect logs and completion behavior
3. if healthy, increase to `2`
4. if still healthy, increase to `4`

Do not start with all manifests at once.

## Step 9: Monitor Jobs And Logs

List jobs:

```bash
gcloud storage batch-operations jobs list
```

Describe a job:

```bash
gcloud storage batch-operations jobs describe gcs-cache-ac-canary-20260614
```

Read logs:

```bash
gcloud logging read \
  'resource.type="storagebatchoperations.googleapis.com/Job"' \
  --limit=50
```

## Step 10: Stop Conditions

Stop the rollout immediately if any of the following occurs:

- unexpected failed transforms exceed `0.1%`
- unexpected failed transforms exceed `1000` objects in one job
- CI cache behavior clearly regresses after the `ac` canary
- manifest-generation mismatch or candidate drift is discovered

Expected non-fatal outcomes:

- object already missing
- generation mismatch against a newer object

Record those separately, but do not automatically treat them as catastrophic.

## Step 11: Post-Delete Verification

After all historical silent manifests finish:

1. request a fresh inventory snapshot
2. rerun the evaluation query against the new snapshot
3. compare:
   - total objects
   - total bytes
   - residual historical silent objects

The expected result is that the historical silent population falls close to
zero, aside from objects that changed after the original snapshot or were
intentionally excluded.

## Optional Cleanup

After verification, remove no-longer-needed temporary tables:

```bash
bq --location=US rm -f -t \
  pingcap-testing-account:ci_bazel_cache_logs._tmp_gcs_cache_delete_candidates_20260614
```

Keep manifest files until the rollout is fully accepted and audited.
