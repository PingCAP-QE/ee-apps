# Cost Schema Retirement Design

Status: Implemented in code; pending staged production rollout  
Date: 2026-08-21

## Summary

Retire the obsolete raw-cost pipeline, remove two stale tables, and prune indexes
that do not support current query paths.

The change is deliberately split into a code release and a later database
cleanup. A binary that still reads `cost_raw_details` must not overlap with the
destructive migration.

## Goals

1. Remove `cost_raw_details` and its obsolete collection/backfill paths.
2. Remove `cost_unattached_ebs_volume_daily`, which has been replaced by
   `cost_unattached_block_volume_daily`.
3. Remove the empty and unused `cost_roster_aliases` table.
4. Remove indexes with no current reader while retaining ingestion,
   idempotency, source/date serving, resource-lineage, and inventory indexes.
5. Make the rollout observable and safe against mixed-version deployments.

## Non-goals

- Redesign `cost_attribution_daily` or the Kubernetes allocation model.
- Rename `cost_bq_export_summary_daily` or
  `cost_unmatched_resource_daily`.
- Remove resource lineage columns or PVC-to-pod attribution.
- Change cost values, attribution rules, or dashboard API responses.
- Optimize every remaining cost query in this migration.
- Remove the EBS-named API, route, CLI, or Python compatibility wrappers. They
  already delegate to the vendor-neutral block-volume implementation and do not
  depend on the retired table; removing those public compatibility names is a
  separate API deprecation.

## Production evidence

The production TiDB `insight` schema was inspected read-only on 2026-08-21.
Counts are approximate because collectors remain active.

| Object | Observation |
| --- | --- |
| `cost_raw_details` | 9,122,095 rows; last updated 2026-05-24; about 2,547 MiB data + 4,414 MiB indexes = 6,961 MiB (about 6.80 GiB) |
| `cost_unattached_ebs_volume_daily` | 6 rows, all matched by the replacement block-volume table; its DDL is not migration-managed in this repository |
| `cost_roster_aliases` | 0 rows and no repository code reference; its DDL is not migration-managed in this repository |
| `cost_sources` | 4 rows; billing-account index has zero recorded use |
| `cost_budgets` | 4 source-wide rows; group/manager/repo indexes have zero recorded use |
| `cost_bq_export_summary_daily` | service, region, and export-partition indexes have zero recorded use |
| `cost_unmatched_resource_daily` | repo and standalone resource-name indexes have zero recorded use; source/date/namespace index is actively used |

`CLUSTER_TIDB_INDEX_USAGE` counters can reset when the TiDB instance restarts.
They are supporting evidence, not sufficient on their own. The rollout therefore
includes a fresh observation window before indexes are dropped.

## Current dependencies to remove

### `cost_raw_details`

Production Dashboard APIs no longer read this table, but the repository still
contains manual compatibility paths:

- `sync-gcp-billing-export`
- `backfill-gcp-cost-refine-from-raw`
- `refresh-cost-attribution-daily`
- `sync_gcp_billing_export.py`
- `backfill_cost_refine_from_raw.py`
- raw-mode SQL in `refresh_attribution_daily.py`
- tests and documentation for those paths

The supported path is now:

```text
GCP/AWS billing export
  -> cost_bq_export_summary_daily
  -> refresh-cost-attribution-from-summary
  -> cost_attribution_daily
```

Resource investigation uses `cost_unmatched_resource_daily`; it does not need
`cost_raw_details`.

### `cost_unattached_ebs_volume_daily`

Current code reads and writes only `cost_unattached_block_volume_daily`.
`get_unattached_ebs_volumes()`, the `/cost-unattached-ebs-volumes` route, and
`sync-unattached-ebs-volumes` are EBS-named compatibility wrappers over the new
block-volume implementation; none reads the old EBS table. They remain in this
change to avoid combining physical table retirement with a public API/CLI
removal. Their later deprecation should be tracked separately.

### `cost_roster_aliases`

The table has no rows and no current code reference. Attribution uses
`roster_employees`, `roster_groups`, provider labels, and current normalization
rules.

## Target schema

### Removed tables

```sql
DROP TABLE IF EXISTS cost_raw_details;
DROP TABLE IF EXISTS cost_unattached_ebs_volume_daily;
DROP TABLE IF EXISTS cost_roster_aliases;
```

Dropping `cost_raw_details` also removes all seven of its secondary indexes and
its unique source-row index.

### Indexes removed in the first cleanup

These indexes have no current query path and zero production usage in the
observed snapshot:

```sql
ALTER TABLE cost_sources
  DROP INDEX IF EXISTS idx_cost_sources_billing_account;

ALTER TABLE cost_budgets
  DROP INDEX IF EXISTS idx_cost_budgets_group,
  DROP INDEX IF EXISTS idx_cost_budgets_manager,
  DROP INDEX IF EXISTS idx_cost_budgets_repo;

ALTER TABLE cost_bq_export_summary_daily
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_service,
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_region,
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_export_partition;

ALTER TABLE cost_unmatched_resource_daily
  DROP INDEX IF EXISTS idx_cost_unmatched_resource_repo,
  DROP INDEX IF EXISTS idx_cost_unmatched_resource_resource_name;
```

`idx_cost_bq_export_summary_export_partition` has no observed current reader.
Source-scoped collector queries can use the unique index beginning with
`(vendor, account_id, export_partition_date, source_row_hash)`, while current
cross-source freshness checks use `usage_date`, not `export_partition_date`.
The unique index does **not** replace the standalone index for a hypothetical
query filtering only by `export_partition_date`, because that column is third
in the composite key. Removal therefore depends on the Phase 2 observation
window confirming that no such query exists; it is not structurally redundant.

### Indexes explicitly retained

| Index | Reason |
| --- | --- |
| `uk_cost_sources_vendor_account` | source identity and upsert |
| `uk_cost_budgets_scope` | budget idempotency and current budget reads |
| `idx_cost_budgets_period` | retain for future period growth; only four rows today |
| `idx_cost_bq_export_summary_usage_date` | attribution rebuild by usage date |
| `idx_cost_bq_export_summary_resource` | current GCP resource/PVC lineage path |
| `uk_cost_bq_export_summary_source_row` | import idempotency |
| `idx_cost_unmatched_resource_usage_date` | retain initially as a rollback/query fallback |
| `idx_cost_unmatched_source_date_namespace` | forced by the current Dashboard unmatched query |
| `uk_cost_unmatched_resource_source_row` | import idempotency |
| attribution source/date and lineage indexes | current Dashboard and residual allocation paths |
| Kubernetes allocation source/date and lineage indexes | current allocation serving paths |
| block-volume indexes | active inventory API and sync paths |

### Deferred index candidates

The following indexes also showed zero usage, but are not removed in this
change because they belong to active serving tables or user-selectable filters:

- attribution author/repo/branch/region/group/manager/project indexes
- `idx_cost_attribution_source_scope_date`
- Kubernetes allocation branch index

They should be evaluated separately with representative `EXPLAIN ANALYZE`
results. This keeps the first cleanup small and avoids combining schema
retirement with a broad serving-index redesign.

## Rollout

### Phase 0: preflight

Before the code release:

1. Confirm no CronJob or one-off Job invokes the three raw commands. Scheduled
   Cost Insight manifests live in the external `ee-ops` repository, currently
   under `apps/gcp/cost-insight/cronjobs.yaml`; inspect that repository **and**
   live GKE CronJobs/Jobs because this repository alone cannot prove the
   commands are unused.
2. Confirm no running pod uses an older image that can write
   `cost_raw_details`.
3. Confirm summary and attribution freshness for every active source.
4. Confirm the TiDB backup/PITR retention covers the destructive migration.
5. Capture table counts, date ranges, and aggregate costs for audit evidence.
6. Capture `SHOW CREATE TABLE` output for all three retired tables in the
   approved rollout artifact store. The EBS and roster-alias tables are
   historical/manual production objects with no authoritative CREATE DDL in
   this repository; their captured production DDL is required for rollback.

Required checks:

```sql
SELECT vendor, account_id, COUNT(*) AS rows_n,
       MIN(usage_date) AS min_date,
       MAX(usage_date) AS max_date,
       ROUND(SUM(list_cost), 2) AS list_cost,
       ROUND(SUM(net_cost), 2) AS net_cost
FROM cost_raw_details
GROUP BY vendor, account_id;

SELECT vendor, account_id,
       MAX(usage_date) AS max_usage_date,
       ROUND(SUM(list_cost), 2) AS list_cost,
       ROUND(SUM(net_cost), 2) AS net_cost
FROM cost_attribution_daily
GROUP BY vendor, account_id;

SHOW CREATE TABLE cost_raw_details;
SHOW CREATE TABLE cost_unattached_ebs_volume_daily;
SHOW CREATE TABLE cost_roster_aliases;
```

Do not commit production `SHOW CREATE TABLE` output blindly: review it and put
it in the approved rollout artifact store. Table DDL normally contains no data
credentials, but the artifact should still follow the operational access
policy.

`cost_raw_details` is derived from BigQuery, but rebuilding it later may not
reproduce byte-for-byte historical rows after query logic changes. Destructive
cleanup therefore requires either accepted data loss for this deprecated layer
or a confirmed TiDB restore point; creating another full TiDB copy is not part
of this design because it would preserve the storage problem.

### Phase 1: compatibility-removal release

Release code that:

1. Removes the `sync-gcp-billing-export` CLI command.
2. Removes the `backfill-gcp-cost-refine-from-raw` CLI command.
3. Removes the `refresh-cost-attribution-daily` raw-mode command.
4. Removes `sync_gcp_billing_export.py` and
   `backfill_cost_refine_from_raw.py`.
5. Removes raw-mode SQL from `refresh_attribution_daily.py`, leaving the
   summary-based implementation as the only attribution path.
6. Removes only raw-mode test cases and fixtures. In particular:
   - edit `tests/test_refresh_attribution_daily.py` to remove raw-mode cases
     while retaining all summary-mode attribution coverage;
   - edit `tests/test_db_and_cli.py` to remove parser/dispatch tests and
     monkeypatches for the three retired commands;
   - delete tests dedicated entirely to `sync_gcp_billing_export.py` and
     `backfill_cost_refine_from_raw.py`.
   Do not delete either mixed-purpose test file wholesale.
7. Updates README and current system-design documentation. Historical design
   documents should be marked superseded rather than rewritten as if the raw
   pipeline never existed.

Do not drop the table in this release. This makes rollback to the immediately
previous application image possible during the observation window.

### Phase 2: observation window

Wait at least seven days after all Cost Insight and Dashboard workloads run the
compatibility-removal release.

During this window verify:

- no statement references `cost_raw_details`;
- no failed job reports a missing raw command or table;
- summary and attribution continue to advance;
- Dashboard Cost endpoints remain healthy;
- fresh `CLUSTER_TIDB_INDEX_USAGE` data still shows zero use for the proposed
  indexes.

Example check:

```sql
SELECT table_name, index_name, query_total, last_access_time
FROM information_schema.cluster_tidb_index_usage
WHERE table_schema = DATABASE()
  AND (
    table_name = 'cost_sources'
    OR table_name = 'cost_budgets'
    OR table_name = 'cost_bq_export_summary_daily'
    OR table_name = 'cost_unmatched_resource_daily'
  )
ORDER BY table_name, index_name;
```

### Phase 3: destructive migration

Apply the idempotent migration manually after the observation window:

```text
cost-insight/sql/015_retire_legacy_cost_schema.sql
```

Execution order:

1. Drop the small stale tables.
2. Drop confirmed-unused indexes.
3. Drop `cost_raw_details` last.

TiDB DDL is not treated as one rollback-capable transaction. Each statement
must be idempotent, and the operator must record which statements succeeded.
Schedule the raw-table drop outside collector and attribution refresh windows
to avoid unnecessary DDL/write overlap.

### Phase 4: post-check

Immediately verify:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name IN (
    'cost_raw_details',
    'cost_unattached_ebs_volume_daily',
    'cost_roster_aliases'
  );
```

Expected result: zero rows.

Verify retained indexes and freshness:

```sql
SELECT table_name, index_name
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name LIKE 'cost\_%'
ORDER BY table_name, index_name;

SELECT vendor, account_id, MAX(usage_date)
FROM cost_attribution_daily
GROUP BY vendor, account_id;
```

Smoke-test:

- Cost trend
- Weekly account summary
- Cost source selector
- Unmatched resources
- Kubernetes allocation overview and records
- Unattached block volumes
- EBS compatibility endpoint `/cost-unattached-ebs-volumes` still returns HTTP
  200 with the same response structure as `/cost-unattached-block-volumes`
- cost data freshness check

Observe TiDB statement errors and API latency for at least one complete daily
collector cycle.

## Rollback

### During Phase 1 or Phase 2

Rollback the application image. `cost_raw_details` still exists, so old manual
commands remain technically usable.

### After index removal

Recreate only an index proven necessary:

```sql
CREATE INDEX idx_cost_sources_billing_account
  ON cost_sources (billing_account_id);
CREATE INDEX idx_cost_budgets_group ON cost_budgets (group_id);
CREATE INDEX idx_cost_budgets_manager ON cost_budgets (manager_id);
CREATE INDEX idx_cost_budgets_repo ON cost_budgets (repo);
CREATE INDEX idx_cost_bq_export_summary_service
  ON cost_bq_export_summary_daily (usage_date, service_name);
CREATE INDEX idx_cost_bq_export_summary_region
  ON cost_bq_export_summary_daily (usage_date, region);
CREATE INDEX idx_cost_bq_export_summary_export_partition
  ON cost_bq_export_summary_daily (export_partition_date);
CREATE INDEX idx_cost_unmatched_resource_repo
  ON cost_unmatched_resource_daily (usage_date, org, repo);
CREATE INDEX idx_cost_unmatched_resource_resource_name
  ON cost_unmatched_resource_daily (resource_name(255));
```

Index recreation is online but can consume resources; recreate only the index
identified by an execution plan or production regression.

### After table removal

- Restore `cost_unattached_ebs_volume_daily` only if rolling back to code older
  than the block-volume migration. Recreate it from the production
  `SHOW CREATE TABLE` artifact captured in Phase 0, because this repository has
  no authoritative CREATE migration for it. Its six observed rows are already
  represented in `cost_unattached_block_volume_daily`; restore row data from
  backup only if the old binary actually requires it.
- Recreate `cost_roster_aliases`, if unexpectedly required, from the captured
  production DDL or TiDB backup. This repository has no authoritative CREATE
  migration for it, and there was no production row data at preflight.
- Restoring `cost_raw_details` requires TiDB PITR/backup. Normal forward
  recovery should instead keep the summary pipeline and fix it; raw-table
  restoration is for emergency rollback only.

## Validation and acceptance criteria

The migration is complete when all of the following are true:

1. No repository runtime code references `cost_raw_details`,
   `cost_unattached_ebs_volume_daily`, or `cost_roster_aliases`.
2. No production workload references the removed objects for seven days before
   destructive cleanup.
3. The three tables no longer exist after Phase 3.
4. All proposed first-cleanup indexes are absent and all explicitly retained
   indexes remain present.
5. Cost source/date totals and freshness do not regress after the code release
   or migration.
6. Cost Dashboard smoke tests pass.
7. No unknown-table or unknown-index errors appear in job/API logs.
8. TiDB storage eventually reflects removal of approximately 6,961 MiB
   (6.80 GiB) from the raw table and its indexes; reclamation need not be
   immediate.

## Implementation files

Expected code release changes:

- `cost-insight/src/cost_insight/jobs/cli.py`
- `cost-insight/src/cost_insight/jobs/refresh_attribution_daily.py`
- delete `cost-insight/src/cost_insight/jobs/sync_gcp_billing_export.py`
- delete `cost-insight/src/cost_insight/jobs/backfill_cost_refine_from_raw.py`
- delete raw-only test modules for those two deleted jobs
- surgically remove raw-mode cases from
  `cost-insight/tests/test_refresh_attribution_daily.py`
- surgically remove the three retired CLI command test groups from
  `cost-insight/tests/test_db_and_cli.py`
- retain all summary-mode attribution and unrelated CLI tests
- `cost-insight/README.md`
- `cost-insight/docs/system-design.md`
- `ci-dashboard/docs/daily-data-freshness-check-design.md`

Destructive migration, committed with the code but applied manually in Phase 3:

- `cost-insight/sql/015_retire_legacy_cost_schema.sql`

Repository and deployment inspection confirms that committing this file does
not apply it: `Dockerfile.jobs` only copies `sql/` into the image, the image
entrypoint is the `cost-insight` CLI, CI/release only build and publish the
image, and the current `ee-ops/apps/gcp/cost-insight/cronjobs.yaml` invokes
application jobs rather than a SQL runner. An operator must execute this file
explicitly after the observation window.

The code release and destructive migration must not be deployed as one atomic
step through an unordered rollout.
