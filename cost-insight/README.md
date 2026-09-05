# Cost Insight

Cost Insight is an independent project for cloud cost and usage collection,
attribution, budget comparison, and cost exploration.

The current implementation supports multiple active sources through
`cost_sources`, including:

- GCP project `pingcap-testing-account`
- GCP project `qa-infra-dev`
- AWS account `946646677266` (`qa-infra-dev`)
- Azure subscription `aaa5414d-7537-4e24-99bd-a7a841221810` (`azure-testing-infra-dev`)
- Azure subscription `abd27163-b965-4217-8cba-2a4c799579fe` (`azure-testing-infra-prod-dataplane`)

Current design:

- [System design](docs/system-design.md)
- [BigQuery cost optimization design](docs/bigquery-cost-optimization-design.md)
- [AWS split-cost source adaptation design](docs/aws-split-cost-schema-migration.md)
- [Target branch cost dimension design](docs/target-branch-cost-dimension-design.md)
- [GCS Bazel cache cleanup design](docs/gcs-bazel-cache-cleanup-design.md)
- [Cost schema retirement design](docs/cost-schema-retirement-design.md)
- [Unified cost allocation design](docs/cost-allocation-unification-design.md)
- [Resource serving materialization design (proposed)](docs/resource-serving-materialization-design.md)

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
| `COST_INSIGHT_SYNC_LAG_DAYS` | `5` |
| `COST_INSIGHT_EXPORT_OVERLAP_DAYS` | `0` |
| `COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS` | unset |
| `COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS` | `5` |
| `COST_INSIGHT_SYNC_PAGE_SIZE` | `5000` |

Allocation publication settings:

| Env | Default |
| --- | --- |
| `COST_ALLOCATION_EARLIEST_DATE` | required |
| `COST_INSIGHT_EQ_ROOT_LARK_GROUP_ID` | required unless passed by CLI |

Useful Azure settings:

| Env | Default |
| --- | --- |
| `COST_INSIGHT_AZURE_BILLING_TABLE` | `gcp-digital-bi.azure_billing.azure_billing_cost_*` |
| `COST_INSIGHT_AZURE_EARLIEST_USAGE_DATE` | `2026-01-01` |
| `COST_INSIGHT_AZURE_SYNC_LAG_DAYS` | `5` |
| `COST_INSIGHT_AZURE_EXPORT_OVERLAP_DAYS` | `0` |
| `COST_INSIGHT_AZURE_SYNC_INITIAL_LOOKBACK_DAYS` | unset |
| `COST_INSIGHT_AZURE_SYNC_PAGE_SIZE` | `5000` |

Azure billing exports are monthly tables with `YYYYMMDD` suffixes. The sync
normalizes requested dates to month starts, accepts at most a five-day CLI
request window, and filters out non-numeric wildcard suffixes.

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

After `sql/001_create_cost_tables.sql` is applied, apply the forward source-purpose
migration before the seed:

```bash
mysql < sql/019_add_cost_source_purpose.sql
mysql < sql/002_seed_initial_cost_sources.sql
```

All recurring summary, unmatched-resource, and attribution jobs discover active
sources from `cost_sources`. The env account IDs are now fallback values for
local validation when the registry table is empty.

## Billing Summary Pipeline

The refined pipeline avoids scanning resource-level billing export columns for
regular dashboard summaries:

```bash
cost-insight sync-gcp-billing-summary \
  --export-partition-start 2026-05-17 \
  --export-partition-end 2026-05-23
```

Azure summary import uses the same `cost_bq_export_summary_daily` table:

```bash
cost-insight sync-azure-billing-summary \
  --account-id aaa5414d-7537-4e24-99bd-a7a841221810 \
  --export-partition-start 2026-04-01 \
  --export-partition-end 2026-04-05
```

Use `--account-id` to import one subscription; omitting it imports both registered
subscriptions. `--replace-existing-partitions` requires explicit partition bounds,
and scoped replacement additionally requires `--replace-usage-start-date`,
`--replace-usage-end-date`, and a single export partition. Each explicit request
may span at most five calendar days.

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

### AWS reconciliation canary

`validate-aws-reconciliation` is a read-only check: it only issues AWS Cost
Explorer, BigQuery, and TiDB `SELECT` requests. It neither runs imports nor
updates job state. It compares the CE `UnblendedCost` stream (Usage and
SavingsPlanCoveredUsage) with the AWS raw export, summary, and attribution
facts after independently rounding each amount to cents.

```bash
cost-insight validate-aws-reconciliation \
  --start-date 2026-08-10 \
  --end-date 2026-08-11 \
  --account-id 296171618728 \
  --tenant 1372813089209272198
```

The caller must use AWS credentials for the payer/management account and have
read access to Cost Explorer; the source table and schema version are read from
`cost_sources` when registered. Cost Explorer uses `us-east-1` by default; pass
`--aws-region` when your AWS setup requires another region. Set
`--tenant-tag-key` if Cost Explorer uses a cost-allocation tag name other than
`tenant`.

Resource-level investigation data is imported separately for a stable usage
week:

```bash
cost-insight sync-gcp-unmatched-resources \
  --usage-start-date 2026-05-17 \
  --usage-end-date 2026-05-23
```

At first native GKE cutover, re-import each affected export partition with
`sync-gcp-billing-summary --replace-existing-partitions`; Kubernetes dimensions
change GKE source hashes, so ordinary upsert is not sufficient. After the
detailed billing export has settled, synchronize native GKE Cost Allocation
residuals. Provider-assigned workload costs pass through unchanged;
idle and system-overhead residuals use direct workload list-cost shares within
the same day, project, cluster, SKU, and component. Unsupported or unknown
residuals remain visible and unallocated.

```bash
cost-insight sync-gcp-kubernetes-workload-allocations \
  --usage-start-date 2026-05-17 \
  --usage-end-date 2026-05-23
```

For a usage-date-bounded historical repair, replace one GCP export partition at
one time. The scoped replacement preserves rows in that partition outside the
requested usage range. BigQuery export partitions may contain late rows for
older usage dates, so the usage range is intentionally independent from the
export partition date:

```bash
cost-insight sync-gcp-billing-summary \
  --account-id pingcap-testing-account \
  --export-partition-start 2026-08-25 \
  --export-partition-end 2026-08-25 \
  --earliest-usage-date 2026-07-01 \
  --replace-existing-partitions \
  --replace-usage-start-date 2026-07-01 \
  --replace-usage-end-date 2026-08-24
```

Build the three derived Dashboard perspectives after Kubernetes facts and
attribution are current. The command keeps the existing active version visible
until the full requested range has conserved successfully.

```bash
cost-insight materialize-cost-allocations \
  --start-date 2026-01-01 \
  --end-date 2026-05-23 \
  --eq-root-lark-group-id <lark-department-id>
```

`COST_INSIGHT_EQ_ROOT_LARK_GROUP_ID` may supply the final argument. The command
requires `--start-date` to equal `COST_ALLOCATION_EARLIEST_DATE` and requires
`--end-date` to cover the latest native cost date. This prevents a partial
version from replacing the global publication pointer. A native-empty date is
intentionally represented by no facts (zero cost); rows are never inherited
from an older version. Rebuild the complete configured history after roster
changes because historical chargeback uses the current organization.

Large rebuilds can stage resumable 4–5 day chunks under one fixed version. A
failed chunk is safe to rerun; only the final command validates every native
window and updates the publication pointer.

```bash
version=allocation_20260823T120000
cost-insight materialize-cost-allocations \
  --start-date 2026-01-01 --end-date 2026-05-23 \
  --processing-start-date 2026-01-01 --processing-end-date 2026-01-05 \
  --allocation-version "$version" --no-publish
# Repeat non-overlapping processing windows, then publish the complete version.
cost-insight materialize-cost-allocations \
  --start-date 2026-01-01 --end-date 2026-05-23 \
  --allocation-version "$version" --publish-only
```

Each materialization window and GKE date replacement logs its percentage and
progress. A GKE replacement commits one usage date atomically: a failure rolls
back that date's delete and writes, while completed dates are safely rerunnable.

AWS unmatched resources use the same investigation table. Successful resource imports and
attribution refreshes automatically republish their affected source/date resource-serving
windows; `materialize-resource-serving` remains available for standalone repair/backfill.

```bash
cost-insight sync-aws-unmatched-resources \
  --usage-start-date 2026-05-17 \
  --usage-end-date 2026-05-23
```

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

Build an index-based dry-run candidate report from the current last-seen table:

```bash
cost-insight cleanup-gcs-cache --mode dry-run --execute-kind cas-from-index
```

The dry-run report does not run the post-delete catch-up or live `by_ac`
recheck used by delete mode, so the CAS delete candidate count is an upper
bound. A real delete may block additional CAS if newly indexed AC refs appear
before the manifest is exported.

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
cost-insight cleanup-gcs-cache \
  --mode delete \
  --execute-kind cas-from-index \
  --max-delete-objects 500
```

Run a real-delete CAS cleanup wave with an explicit hard cap. The job performs
one full `by_cas` rebuild, prefers orphan CAS, only expands linked ACs when the
orphan backlog no longer fills the CAS budget, and rechecks live `by_ac`
references before exporting the CAS delete manifest:

```bash
cost-insight cleanup-gcs-cache \
  --mode delete \
  --execute-kind cas-from-index \
  --max-delete-objects 10000000 \
  --max-delete-ac-objects 100000
```
