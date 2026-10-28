# Account-scoped BigQuery cost freshness

## Goal and boundaries

Extend the existing `check-data-freshness` daily CronJob, not a second CronJob.
For every active `cost_sources` identity, show BigQuery availability, imported
summary and attribution watermarks together. One healthy account must not hide
another stalled account behind a table-wide `MAX(usage_date)`.

This is **read-only watermark observation**, not daily completeness, resource
coverage or amount reconciliation. A newest date can coexist with historical
holes or a partially published day. No importer, backfill, IAM change, schema
migration or automatic repair runs from this checker.

The existing SQL checks remain unchanged. The new comparison is opt-in, so an
older CronJob identity without BigQuery access does not unexpectedly start
cloud queries when the application image is upgraded.

## Result contract

Each row includes `vendor/account_id`, source scope/profile, bounded observation
window, BigQuery `source_latest`, importer-eligible `eligible_latest`, local
`summary_latest` and `attribution_latest`. Dates are UTC calendar dates.

Signed differences remain visible:

- `raw-summary = source_latest - summary_latest`
- `eligible-summary = eligible_latest - summary_latest`
- `summary-attr = summary_latest - attribution_latest`

Local dates ahead of the scheduled eligibility window are legitimate after an
ad hoc import. Dates ahead of the **actual observed source** are instead an
explicit `local_ahead` warning: different source definitions or bounded-window
limitations need investigation, not clamping to a healthy zero.

Registry, local SQL, ADC, permission, missing table/column, timeout and query
budget failures become visible failed results. Failure of one vendor must not
prevent the other vendors or the original CI checks from being reported.

## Sources and date semantics

The query project and all source tables belong to the explicitly configured
warehouse project (`warehouse-f80fbbe8e8` in production), in `us-west1`.

| Vendor | Stable relation | Scope / date |
|---|---|---|
| GCP | `raw_gcp_billing.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6` | `project.id`, `DATE(usage_start_time)`; bound ingestion `_PARTITIONDATE` |
| Azure | `raw_azure_billing.azure_billing_cost_*` | `SubscriptionId`, `date`; numeric month-first table suffixes only (MCA), not EA `cn_`/`us_` tables |
| Alibaba | `raw_alibaba_billing.daily_en_*` | `owner_account_id`, `DATE(usage_start_time)`; export suffix and `partition_date` are separate bounds |
| AWS ordinary CUR | `mart_cloud_cost.aws_member_daily_cost` | USD account-level availability; `usage_date`; physical `billing_period` bounds required |

AWS ordinary CUR accepts all line-item types in its existing importer; the
availability query does not quietly switch to a Usage-only row domain. It is
an account-level availability comparison, not a claim that the mart contains
all importer dimensions or identical monetary facts.

`aws_split_cost_v1`, `aws_tidb_cloud_f04_v1` and other unverified profiles retain
account-wide source dates **only as diagnostics**. The mart lacks split lineage
and F04's `usedby=prod-us-west-2-f04` tenant boundary. These rows are
`scope_unverified` (LOW, not healthy), pending an authorized stable source with
that exact scope. Local summary-to-attribution mismatches can still be reported
as real failures. Expiring `_stage_*` tables are never scheduled sources.

Tencent uses its billing API, not BigQuery. Its row is explicitly `non_bigquery`
and is not added to the healthy comparison count. Unknown vendors/profiles
must not be guessed into a supported scope.

### Eligibility and the existing morning schedule

The deployed freshness check is at **09:10 Asia/Shanghai**, before the daily
cost importers around noon. Use the previous UTC calendar day as the last
scheduled importer day, then apply its export lag:

| Vendor | Eligible export cutoff |
|---|---|
| GCP | previous UTC day minus 3 days; restrict which ingestion partitions contribute to `eligible_latest` |
| Alibaba | previous UTC day minus 5 days; restrict export/partition dates contributing to `eligible_latest` |
| Azure | month containing previous UTC day minus 3 days; eligible month tables can contain usage later than that cutoff |
| AWS ordinary | current bounded account availability; one-day ingestion allowance |

The policy mirrors this deployment's cadence; moving the check after the
importers or changing their lag requires reviewing this policy together.
Ingestion is delayed when `eligible-summary > 1 day`; the one-day grace avoids
calling an export update between daily runs a failed import. Attribution must
keep up with the available summary (zero-day allowance). An attribution date
newer than summary is also a lineage anomaly.

A new source with no eligible export yet and no local rows is
`waiting_import_lag`, not an ingestion failure or a healthy check. Empty source
windows and missing expected local rows are explicit failures. A source older
than seven days that local data has caught up with is
`source_stale_or_no_recent_usage`: MAX dates alone cannot distinguish upstream
stoppage from a legitimate period without consumption. If summary still trails
that stale source within the one-day ingestion allowance, it is instead
`source_stale_with_import_gap` (failed); the allowance must not hide source staleness.

Alibaba prepaid renewals illustrate why export and usage cannot be conflated:
a real USD 157 charge was exported/paid on October 14 but starts usage on
October 17. This checker follows the existing importer's usage date, not the
mart's billing-day placement. Export observation includes the previous month
where possible, capped at 62 days. It does not claim an unbounded historical
MAX or infer completeness for prepaid usage exported outside that window.

## Execution and limits

- Enumerate active sources from the shared TiDB registry. Preserve excluded,
  unsupported and missing identities in the result rather than silently
  dropping them.
- Read local summary and attribution dates per vendor/account in a bounded
  window. Reuse existing source/date indexes; do not hold the DB connection
  across ADC or BigQuery requests.
- Batch account IDs into at most four vendor BigQuery queries. Bind account
  arrays and date parameters; warehouse relation names are fixed, with a
  validated project ID.
- GCP uses ingestion partition bounds, AWS uses monthly billing partitions,
  and Azure/Alibaba use explicit numeric suffix ranges. No `LIMIT` is used as
  a substitute for scan control.
- Dry-run every query before execution. Both stages set the hard byte cap;
  execution never automatically increases it. An unknown/oversized estimate
  is a failed result. Each query/result operation has a deadline and no
  unbounded SDK retry; successful queries may use BigQuery's valid cache.
- Default lookback: 14 usage days. Default cap: **2 GiB per vendor query**
  (at most 8 GiB/run before cache savings). The minimal GCP 14-day/5-project
  query was dry-run at 1,118,126,285 bytes on October 28, just over 1 GiB;
  therefore the default is a measured bound rather than an always-failing
  1 GiB configuration. This is not permission to increase it on failure.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FRESHNESS_COST_BQ_ENABLED` | `false` | Opt in to source comparisons and the daily cost report |
| `FRESHNESS_COST_BQ_PROJECT` | unset | Required explicit warehouse/query project when enabled |
| `FRESHNESS_COST_BQ_LOCATION` | `us-west1` | Explicit BigQuery query location |
| `FRESHNESS_COST_BQ_LOOKBACK_DAYS` | `14` | Bounded usage window; validated 8..62 |
| `FRESHNESS_COST_BQ_MAXIMUM_BYTES_BILLED` | `2147483648` | Per-query hard cap, including dry-run estimate validation |
| `FRESHNESS_COST_BQ_TIMEOUT_SECONDS` | `30` | Individual query/result operation timeout |
| `FRESHNESS_COST_BQ_EXCLUDED_SOURCES` | empty | Explicit comma-separated `vendor/account` exceptions |

Exclusions are printed, not counted as healthy. For example, the production
Azure subscription `azure/abd27163-b965-4217-8cba-2a4c799579fe` was independently
confirmed to have no new consumption since June 24; if operators approve
excluding it, declare that exception rather than inferring dormancy from an
empty query or changing `cost_sources.is_active` from this job.

When enabled, the existing Lark notifier sends one daily report including all
cost rows, even when healthy. Logs also contain every comparison. Existing
failure-only notifications remain unchanged when disabled. `FRESHNESS_DRY_RUN`
still performs the bounded reads, but prints the report and sends no message.
Source failures participate in the existing failed count and nonzero CLI exit.
Explicit skips/pending eligibility do not inflate the healthy count.

## Deployment and rollback

1. Merge/release the application change and verify the published
   `ci-dashboard-jobs` version tag. Do not invent a future image tag.
2. Update **only** the existing freshness CronJob in ee-ops: use its shared DB
   and Lark secrets unchanged; set the explicit project/location and enable
   the feature. Use `serviceAccountName: cost-insight-gcp-billing` in `apps`:
   this existing dedicated KSA is directly bound to the approved warehouse
   GSA. No self-impersonation, shared-SA TokenCreator role or project-wide
   IAM expansion is needed. Validate 09:10 scheduling and allow a 900-second
   job deadline if operators want room for multiple timeout reports.
3. Use `scripts/render_data_freshness_check_cronjob.sh --image <released-tag>
   --service-account cost-insight-gcp-billing --cost-bq-project warehouse-f80fbbe8e8`
   as a rendering example. The script requires an explicit KSA when enabling
   cloud reads; do not apply its unrelated defaults over existing production
   DB/Lark settings. Production uses Lark app credentials and an open_id.
4. Roll back by setting `FRESHNESS_COST_BQ_ENABLED=false`. Original SQL checks
   and failure-only notification semantics remain available. This does not
   resume paused importers or alter existing backfill safety holds.

This PR prepares application behavior and rendering, not a production rollout.
No new canary CronJob, source mutation, production configuration or alert send
is part of implementation validation. Offline tests cover scope, bound/lag
semantics and fault isolation; any live validation must be SELECT-only with
notifications disabled or by directly invoking the read-only collector.
