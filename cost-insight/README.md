# Cost Insight

Cost Insight is an independent project for cloud cost and usage collection,
attribution, budget comparison, and cost exploration.

The current implementation supports multiple active sources through
`cost_sources`, including:

- GCP project `pingcap-testing-account`
- GCP project `qa-infra-dev`
- AWS account `946646677266` (`qa-infra-dev`)

Current design:

- [System design](docs/system-design.md)
- [BigQuery cost optimization design](docs/bigquery-cost-optimization-design.md)
- [Target branch cost dimension design](docs/target-branch-cost-dimension-design.md)
- [GCS Bazel cache cleanup design](docs/gcs-bazel-cache-cleanup-design.md)

## Local Setup

```bash
cd cost-insight
python -m pip install -e '.[dev]'
```

The collector reads database settings from `COST_INSIGHT_DB_URL` first, then
falls back to `COST_DB_URL`, `CI_DASHBOARD_DB_URL`, `COST_INSIGHT_TIDB_*`,
`COST_TIDB_*`, or `TIDB_*`.

Useful GCP settings:

| Env | Default |
| --- | --- |
| `COST_INSIGHT_GCP_BILLING_TABLE` | `gcp-digital-bi.gcp_billing_detailed.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6` |
| `COST_INSIGHT_GCP_ACCOUNT_ID` | `pingcap-testing-account` |
| `COST_INSIGHT_EARLIEST_USAGE_DATE` | `2026-01-01` |
| `COST_INSIGHT_SYNC_OVERLAP_DAYS` | `3` |
| `COST_INSIGHT_SYNC_LAG_DAYS` | `5` |
| `COST_INSIGHT_EXPORT_OVERLAP_DAYS` | `0` |
| `COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS` | unset |
| `COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS` | `5` |
| `COST_INSIGHT_SYNC_PAGE_SIZE` | `5000` |

Useful AWS settings:

| Env | Default |
| --- | --- |
| `COST_INSIGHT_AWS_BILLING_TABLE` | `gcp-digital-bi.stg_cloud_billing.stg_aws_billing` |
| `COST_INSIGHT_AWS_ACCOUNT_ID` | unset |
| `COST_INSIGHT_AWS_EARLIEST_USAGE_DATE` | `2026-01-01` |
| `COST_INSIGHT_AWS_EXPORT_OVERLAP_MONTHS` | `1` |
| `COST_INSIGHT_AWS_SYNC_INITIAL_LOOKBACK_MONTHS` | `2` |
| `COST_INSIGHT_AWS_SYNC_PAGE_SIZE` | `5000` |

The Python BigQuery SDK requires Application Default Credentials. For local
validation with a user account:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project pingcap-testing-account
```

## Seed Active Sources

After `sql/001_create_cost_tables.sql` is applied:

```bash
mysql < sql/002_seed_initial_cost_sources.sql
```

All recurring summary, unmatched-resource, and attribution jobs discover active
sources from `cost_sources`. The env account IDs are now fallback values for
local validation when the registry table is empty.

## GCP Raw Backfill

```bash
cost-insight sync-gcp-billing-export --start-date 2026-01-01 --end-date 2026-05-17 --split-by-day
```

For a small validation run:

```bash
cost-insight sync-gcp-billing-export --start-date 2026-05-17 --end-date 2026-05-17 --limit 100 --dry-run
```

`--dry-run` reads BigQuery and normalizes rows but does not write
`cost_raw_details` or advance `cost_job_state`.

## Attribution Refresh

After raw details are imported, rebuild the daily attribution table for the
affected date range:

```bash
cost-insight refresh-cost-attribution-daily --start-date 2026-05-09 --end-date 2026-05-17 --split-by-day
```

For a safe validation first:

```bash
cost-insight refresh-cost-attribution-daily --start-date 2026-05-09 --end-date 2026-05-17 --split-by-day --dry-run
```

This job reads `cost_raw_details`, joins current `roster_employees` and
`roster_groups`, then rebuilds `cost_attribution_daily` for the requested
`vendor/account/date` range. It is intentionally rerunnable so late billing
corrections and roster fixes can be reflected by refreshing the same dates.
Use `--split-by-day` for multi-day ranges to stay under TiDB single-query
memory limits.

## BigQuery Cost-Optimized Pipeline

The refined pipeline avoids scanning resource-level billing export columns for
regular dashboard summaries:

```bash
cost-insight sync-gcp-billing-summary \
  --export-partition-start 2026-05-17 \
  --export-partition-end 2026-05-23
```

AWS summary import uses the same `cost_bq_export_summary_daily` table:

```bash
cost-insight sync-aws-billing-summary \
  --export-partition-start 2026-05-01 \
  --export-partition-end 2026-05-01
```

After summary rows are imported, refresh attribution from the summary table:

```bash
cost-insight refresh-cost-attribution-from-summary \
  --start-date 2026-05-17 \
  --end-date 2026-05-23 \
  --split-by-day
```

Resource-level investigation data is imported separately for a stable usage
week:

```bash
cost-insight sync-gcp-unmatched-resources \
  --usage-start-date 2026-05-17 \
  --usage-end-date 2026-05-23
```

AWS unmatched resources use the same investigation table:

```bash
cost-insight sync-aws-unmatched-resources \
  --usage-start-date 2026-05-17 \
  --usage-end-date 2026-05-23
```

To avoid a BigQuery backfill during migration, seed the new tables from the
existing `cost_raw_details` table:

```bash
cost-insight backfill-gcp-cost-refine-from-raw \
  --start-date 2026-01-01 \
  --end-date 2026-05-20 \
  --mark-summary-watermark
```

The backfill synthesizes `export_partition_date` from
`DATE(source_export_time)`, falling back to `usage_date` when
`source_export_time` is missing. `--mark-summary-watermark` prevents the new
summary importer from scanning already-backfilled historical export partitions.

See [docs/bigquery-cost-optimization-design.md](docs/bigquery-cost-optimization-design.md)
for the detailed table design, query shapes, and cost estimates.

## GCS Bazel Cache Cleanup

Summarize one day of access logs into BigQuery object last-seen tables:

```bash
cost-insight sync-gcs-cache-last-seen --run-date 2026-06-08
```

Bootstrap the current last-seen table from the historical audit-log window in
one scan:

```bash
cost-insight bootstrap-gcs-cache-last-seen --start-date 2026-05-25 --end-date 2026-06-09
```

This command rebuilds `gcs_cache_object_last_seen_current` directly from the
raw audit-log window. It is intended for one-time historical seeding before the
daily incremental sync continues.

Validate the query shape without writing BigQuery summary tables:

```bash
cost-insight sync-gcs-cache-last-seen --run-date 2026-06-08 --dry-run
```

Build a daily steady-state dry-run candidate report from the current last-seen
table:

```bash
cost-insight cleanup-gcs-cache --mode dry-run
```

Override retention windows during validation:

```bash
cost-insight cleanup-gcs-cache \
  --mode dry-run \
  --ac-retention-days 14 \
  --cas-retention-days 21 \
  --safety-buffer-days 1
```

Run a real-delete steady-state canary with `500 ac + 500 cas`:

```bash
cost-insight cleanup-gcs-cache --mode delete --execute-kind mixed-canary
```

Run a real-delete `ac` cleanup wave with an explicit hard cap:

```bash
cost-insight cleanup-gcs-cache \
  --mode delete \
  --execute-kind ac \
  --max-delete-objects 10000000
```
