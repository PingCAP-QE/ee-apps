# Cost Insight

Cost Insight is an independent project for cloud cost and usage collection,
attribution, budget comparison, and cost exploration.

The first implementation target is GCP project `pingcap-testing-account`. The
design stays vendor-neutral so AWS CUR and other vendors can land in the same
logical model later.

Current design:

- [System design](docs/system-design.md)

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
| `COST_INSIGHT_SYNC_OVERLAP_DAYS` | `3` |
| `COST_INSIGHT_SYNC_PAGE_SIZE` | `5000` |

The Python BigQuery SDK requires Application Default Credentials. For local
validation with a user account:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project pingcap-testing-account
```

## First GCP Backfill

After `sql/001_create_cost_tables.sql` is applied:

```bash
mysql < sql/002_seed_initial_cost_sources.sql
```

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
