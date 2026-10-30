# Cost Insight

Cost Insight is an independent project for cloud cost and usage collection,
attribution, budget comparison, and cost exploration.

The current implementation supports multiple active sources through
`cost_sources`, including:

- GCP project `pingcap-testing-account`
- GCP project `qa-infra-dev`
- AWS account `946646677266` (`qa-infra-dev`)
- AWS account `131464424160` (`qa-infra-prod`, Essential V2 canary-release PRD)
- Azure subscription `aaa5414d-7537-4e24-99bd-a7a841221810` (`azure-testing-infra-dev`)
- Azure subscription `abd27163-b965-4217-8cba-2a4c799579fe` (`azure-testing-infra-prod-dataplane`)
- Alibaba Cloud owner account `5028760335873601` (`alicloud-testing-infra-dev`)
- Tencent Cloud organization account `100050658403` (disabled until validated)

Current design:

- [System design](docs/system-design.md)
- [BigQuery cost optimization design](docs/bigquery-cost-optimization-design.md)
- [AWS split-cost source adaptation design](docs/aws-split-cost-schema-migration.md)
- [Alibaba billing import design](docs/alibaba-billing-import-design.md)
- [Tencent billing import design](docs/tencent-billing-import-design.md)
- [Tencent CI shared-cost allocation](docs/tencent-ci-native-shared-cost-allocation-design.md)
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
| `COST_INSIGHT_GCP_BILLING_TABLE` | `warehouse-f80fbbe8e8.raw_gcp_billing.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6` |
| `COST_INSIGHT_GCP_ACCOUNT_ID` | `pingcap-testing-account` |
| `COST_INSIGHT_EARLIEST_USAGE_DATE` | `2026-01-01` |
| `COST_INSIGHT_SYNC_LAG_DAYS` | `5` |
| `COST_INSIGHT_EXPORT_OVERLAP_DAYS` | `0` |
| `COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS` | unset |
| `COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS` | `5` |
| `COST_INSIGHT_SYNC_PAGE_SIZE` | `5000` |

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
request window, and filters out non-numeric wildcard suffixes. A non-empty Azure
`tenant` resource tag is projected to summary `org` for attribution matching,
while the normalized tag object remains in `vendor_tags_json` for lineage.

Useful Alibaba Cloud settings:

| Env | Default |
| --- | --- |
| `COST_INSIGHT_ALIBABA_BILLING_TABLE` | `gcp-digital-bi.alibaba_cloud.daily_en_*` |
| `COST_INSIGHT_ALIBABA_ACCOUNT_ID` | `5028760335873601` |
| `COST_INSIGHT_ALIBABA_EARLIEST_USAGE_DATE` | `2026-01-01` |
| `COST_INSIGHT_ALIBABA_SYNC_LAG_DAYS` | `5` |
| `COST_INSIGHT_ALIBABA_EXPORT_OVERLAP_DAYS` | `0` |
| `COST_INSIGHT_ALIBABA_SYNC_INITIAL_LOOKBACK_DAYS` | unset |
| `COST_INSIGHT_ALIBABA_SYNC_PAGE_SIZE` | `5000` |

Useful Tencent Cloud settings:

| Env | Default |
| --- | --- |
| `COST_INSIGHT_TENCENT_ACCOUNT_ID` | `100050658403` |
| `COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY` | required for the first scheduled run |
| `COST_INSIGHT_TENCENT_IMPORT_LAG_DAYS` | `3` |
| `COST_INSIGHT_TENCENT_VERIFY_LAG_DAYS` | `5` |
| `COST_INSIGHT_TENCENT_PAGE_SIZE` | `100` |

Tencent credentials use the SDK environment variables `TENCENTCLOUD_SECRET_ID` and
`TENCENTCLOUD_SECRET_KEY`. The source seeded by `sql/025_add_tencent_billing_cost_source.sql`
starts disabled; enable it only after an authenticated dry run and currency migration validation.
Set its `source_available_from` migration value to the same date as
`COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY`.

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

### Warehouse billing identity and source cutover

Billing exporters (including unmatched resources and reconciliation) support these
optional settings; unset values preserve ADC client defaults:

| Env | Meaning |
| --- | --- |
| `COST_INSIGHT_BILLING_BQ_PROJECT` | Query-job and impersonation quota project; required with impersonation |
| `COST_INSIGHT_BILLING_BQ_LOCATION` | Query location, e.g. `us-west1` (not `US`) |
| `COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT` | Target SA for short-lived, keyless impersonation |

Use [config/warehouse-billing.env.example](config/warehouse-billing.env.example)
as a **staged** profile. It defaults to direct Workload Identity and explicitly
unsets impersonation, so a pod already bound to the target GSA does not request
a self-impersonation hop. The target needs query-job permission and table access;
its approved access expires on **2027-04-25**.

For local ADC, explicitly opt in **after** sourcing the profile:

```bash
source config/warehouse-billing.env.example
export COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT=hxprog-e5d756e67e8641f386ab0e9@warehouse-f80fbbe8e8.iam.gserviceaccount.com
```

That local/cross-identity caller needs permission to impersonate the target SA.
An explicit billing query project is required with impersonation; missing it
fails before credential/client creation instead of charging an ambient ADC
project. The shared `ci-dashboard` GSA was verified to lack
`iam.serviceAccounts.getAccessToken` on this target. Billing settings do not
change GCS cache storage/BigQuery clients or their project.

The GCP default now points at the schema-compatible warehouse raw export. The new
comparison sources for the other vendors are:

- `warehouse-f80fbbe8e8.mart_cloud_cost.aws_member_daily_cost`
- `warehouse-f80fbbe8e8.mart_cloud_cost.azure_daily_cost`
- `warehouse-f80fbbe8e8.mart_cloud_cost.alibaba_daily_cost`

AWS query builders support explicit `warehouse_raw=True` for full CUR data.
The legacy import commands retain their existing source routing. The separate
`sync-warehouse-billing-summary` command below uses permanent raw warehouse
sources for ordinary AWS and Azure accounts.

These marts are **not drop-in importer sources**: they omit detail/attribution
fields (especially AWS tenant/usedby and split-cost lineage). Do not change the
existing AWS source profiles or point legacy importers at them before the
small-sample amount and dimension checks pass. Temporary `_stage`/`_before`
tables are not stable source contracts either. Keep legacy billing reads in a
separate invocation/profile because their regions and access identities differ.
No backfill or production-writer cutover is implied by this profile. See the
[2026-10-27 cutover validation report](docs/warehouse-billing-cutover-2026-10-27.md)
for the AWS amount/dimension checks and remaining cutover gates.

## Seed Active Sources

After `sql/001_create_cost_tables.sql` is applied, apply the forward source-profile
and source-purpose migrations before the seed:

```bash
mysql < sql/010_add_aws_split_cost_dimensions.sql
mysql < sql/019_add_cost_source_purpose.sql
mysql < sql/002_seed_initial_cost_sources.sql
mysql < sql/027_add_cost_source_category.sql
mysql < sql/024_add_cost_currency.sql
mysql < sql/025_add_tencent_billing_cost_source.sql
```

Legacy recurring summary, unmatched-resource, and attribution jobs discover active
sources from `cost_sources`. Their env account IDs are fallback values for
local validation when the registry table is empty. The warehouse daily command
instead requires explicit account IDs and validates each against the registry.

## Warehouse Daily Synchronization

Run the same image command for ordinary AWS or Azure sources:

```bash
cost-insight sync-warehouse-billing-summary --vendor aws \
  --account-id 131464424160 --account-id 296171618728 --dry-run
cost-insight sync-warehouse-billing-summary --vendor azure \
  --account-id aaa5414d-7537-4e24-99bd-a7a841221810 --dry-run
```

Set `COST_INSIGHT_BILLING_BQ_PROJECT` and `COST_INSIGHT_BILLING_BQ_LOCATION`
explicitly. This command also uses that project for source table names:
`raw_aws_billing.cur_line_items` or numeric monthly
`raw_azure_billing.azure_billing_cost_YYYYMM01` tables. Azure uses explicit MCA
physical tables, never a mixed-schema wildcard query. Missing/expiring tables,
inactive/custom/split profiles, non-USD data and missing source days fail closed.
It does not change registry entries or resume legacy importers.

For each explicitly requested account, discover the latest actual usage date
not later than UTC yesterday, then reread its trailing **five days** sequentially.
Discovery is limited to 14 days and rejects a source more than seven days old.
Every query is dry-run first with a fixed **2 GiB per-query** cap and finite
timeouts; source metadata must remain stable. Canonical hash collisions are
merged before exact account/billing-partition/day replacement. All source
windows are validated before facts are written. Each day then refreshes
attribution and verifies the active native publication conserves all four
monetary fields. Other accounts, sibling days and unqueried partitions remain
untouched. Recent billing dates remain provisional; revisions older than five
days require a separately reviewed repair rather than a wider automatic scan.

`--dry-run` performs source reads and database SELECTs only, including no job-state
writes. Removing it enables writes **inside GKE only**. Progress and failure state
use the existing `sync_aws_warehouse_daily` / `sync_azure_warehouse_daily` job keys,
so migration cannot bypass a running or failed ConfigMap worker. An oversized
local backlog, failure or interrupted worker requires operator inspection; there
is no automatic replay. Inspect the exact account/day and repair/reconcile facts,
attribution and publication before explicitly releasing its job-state latch.

Deploy the application image first, validate both dry runs, then switch both ops
CronJobs to this command and remove mounted workers. Preserve legacy-source holds
and avoid overlapping old/new writers during the switch.

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

Alibaba Cloud summary import uses the same `cost_bq_export_summary_daily` table:

```bash
cost-insight sync-alibaba-billing-summary \
  --account-id 5028760335873601 \
  --export-partition-start 2026-08-01 \
  --export-partition-end 2026-08-31 \
  --earliest-usage-date 2026-08-01
```

The importer selects `owner_account_id`, preserves `partition_date` as the export
partition independently from `DATE(usage_start_time)`, and maps the Alibaba gross
amount to `list_cost`. See [Alibaba billing import design](docs/alibaba-billing-import-design.md)
for all dimensions and amount mappings.

Tencent organization billing import uses page checkpoints and preserves invoice CNY:

```bash
cost-insight sync-tencent-billing-summary \
  --bill-day-start 2026-09-13 \
  --bill-day-end 2026-09-13 \
  --dry-run
```

The scheduled command imports the next D+3 `BillDay`; explicit ranges are for dry-run,
backfill, or repair. See [Tencent billing import design](docs/tencent-billing-import-design.md)
for the D+5 verification and month-close reconciliation behavior.

Allocate the CI account separately after import. Historical rows need reimporting first so
`vendor_tags_json` contains the stable ProductCode metadata and `project` is populated from
the billing-time `project` label or, when absent, `service`. Allocation preserves each cloud
service and Project, and resource serving projects the daily build weights back onto the
original Tencent resource IDs and labels.

```bash
cost-insight allocate-tencent-ci-cost --start-date 2026-09-13 --end-date 2026-09-13
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

The derived Kubernetes/EQ allocation perspectives are retired. Dashboard cost
queries use native attribution, and resource drilldown uses only the native
resource-serving projection. After removing the old writer schedule and
deploying a binary without its CLI, apply
`sql/026_retire_materialized_cost_allocations.sql` to drop the unused
`cost_allocation_daily` and `cost_allocation_publication` tables.

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
