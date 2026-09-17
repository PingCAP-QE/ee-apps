# Tencent Cloud organization billing import

Status: Implemented in code; migration and production rollout pending

## Context

Cost Insight needs Tencent Cloud cost at resource and allocation-tag granularity without
waiting for a closed monthly bill. A three-to-five-day freshness delay is acceptable. The
normal collector must not repeatedly scan or rewrite already stable data.

The Tencent account currently exposes the required rows only through
`DescribeBillDetailForOrganization`. Read-only validation on 2026-09-16 found:

- ordinary `DescribeCostDetail` and `DescribeCostExplorerSummary` returned no usable rows;
- `DescribeBillDetailForOrganization` returned resource IDs, component costs, and allocation
  tags;
- a one-day `BeginTime` / `EndTime` request returned only that `BillDay`, despite the API also
  supporting month queries;
- 2026-08-10 contained 1,521 rows across 16 pages;
- 2026-09-11 contained 9,169 rows across 92 pages;
- 2026-09-13 contained 4,456 rows across 45 pages;
- the API page limit is 100, and the returned `Context` can accelerate the next page;
- a candidate component identity was unique for all 1,521 components tested on 2026-08-10.

The account is configured for **by-used-time** billing: the console displays “按计费周期（按资源
使用时间统计生成月度账单）”. This matters because delayed usage can be assigned to an older
`BillDay`.

Tencent documents that L3 details normally appear within 20–30 minutes after deduction, while
the monthly bill is final only at 19:00 on the first day of the following month. Tencent also
documents that delayed usage reporting or re-settlement can occur:

- [Bill viewing](https://cloud.tencent.com/document/product/555/96169)
- [Billing FAQ](https://cloud.tencent.com/document/product/555/7465)

Therefore D+3 is a suitable freshness target, but it is not a documented immutability boundary.
The design uses a cheap D+5 verification instead of blindly rereading recent full partitions.

## Goals

- Import organization-paid Tencent L3 billing details every day.
- Make D+3 bill-day data available to the dashboard.
- Read and write one Tencent API page at a time.
- Resume after failure from the last committed page.
- Preserve resource IDs and bill-time allocation tags.
- Store Tencent amounts in CNY and display cross-cloud totals in USD at a fixed
  `6.5 CNY/USD` rate.
- Avoid normal-path month scans, rolling full-day overlap, and partition replacement.
- Preserve exact billed-cost reconciliation and idempotent replay.

## Non-goals

- Using monthly downloadable bills as the regular source.
- Backfilling current resource tags onto historical billing rows.
- Treating D+3 data as invoice-final.
- Building a general foreign-exchange service or historical FX-rate table.
- Persisting the complete Tencent JSON response.
- Allocating untagged shared native-node cost to Pods in this importer.

## Source choice

### Primary source

Use `DescribeBillDetailForOrganization` with an exact Beijing bill day:

```text
BeginTime = YYYY-MM-DD 00:00:00
EndTime   = YYYY-MM-DD 23:59:59
Offset    = 0, 100, 200, ...
Limit     = 100
```

This endpoint is the only source verified to return the required organization-paid detail rows.
Do not substitute ordinary billing, Cost Explorer, or current-resource Tag APIs.

### Why not downloadable bills

`DescribeBillDownloadUrl` has not been proven to represent the same organization-paid scope,
and a monthly file cannot meet the D+3 freshness requirement. It is not part of V1.

### Why not current-resource tags

The `Tags` in the organization bill are the authoritative bill-time allocation snapshot. Looking
up the resource's current tags would rewrite history when tags change and would incorrectly label
historical costs.

## Partition model

Treat `BillDay` as the source arrival partition and `FeeBeginTime` as the usage date:

| Tencent field | Cost Insight field | Meaning |
| --- | --- | --- |
| `BillDay` | `export_partition_date` | Incremental billing partition |
| `DATE(FeeBeginTime)` | `usage_date` | Source usage/service-start date |
| `PayTime` | `source_export_time` | Latest source settlement time |

This is analogous to the GCP model:

```text
GCP _PARTITIONDATE  → export_partition_date
GCP usage_start_time → usage_date
```

A late charge or adjustment can arrive in a newer partition while referring to older usage. The
attribution refresh must therefore use the distinct `usage_date` values written by a completed
bill-day partition, not assume that `usage_date == BillDay`.

## Schedule and freshness

Run once per day after the existing billing collectors, in `Asia/Shanghai`:

```yaml
schedule: "30 13 * * *"
timeZone: Asia/Shanghai
concurrencyPolicy: Forbid
```

Define:

```text
import_cutoff = Beijing today - 3 days
verify_day    = Beijing today - 5 days
```

For each active Tencent cost source:

1. Resume any in-flight bill day.
2. Import every missing bill day through `import_cutoff`, oldest first.
3. Probe every completed, unverified bill day through `verify_day`, oldest first.
4. Refresh attribution and serving data only for fully completed partitions.

The normal path reads a completed partition only through the one-request D+5 count probe. A
smaller count permits the bounded retry and read-only confirmation scan defined below. Backfill
uses the same pagewise path with an explicit bill-day range.

## Pagewise import state machine

A whole day must not be buffered and committed as one large transaction. The read page, write
batch, and checkpoint are the same recovery unit.

Store source progress in `cost_job_state.watermark_json`:

```json
{
  "last_completed_bill_day": "2026-09-12",
  "inflight_bill_day": "2026-09-13",
  "next_offset": 1700,
  "context": "opaque Tencent context",
  "outer_rows_written": 1700,
  "component_rows_written": 1700,
  "expected_total": 4456
}
```

The existing `state_store` lifecycle helpers are not page checkpoints: `mark_job_started`,
`mark_job_succeeded`, and `mark_job_failed` also change `last_status` and lifecycle timestamps.
Add a `checkpoint_job_watermark` helper that updates only `watermark_json` and `updated_at` on an
existing running job. Call it with the same connection and transaction as the page upsert; never
call `mark_job_succeeded` for an intermediate page.

### Fetch loop

For each page:

1. Request `Limit=100`, the persisted `next_offset`, and the previous response `Context` when
   available.
2. Retry the same request with bounded exponential backoff for transient Tencent errors.
3. If a persisted Context is rejected after a process restart, retry the same offset without it;
   Context is an accelerator, not the source checkpoint.
4. Require every returned row's `BillDay` to equal the requested date.
5. Expand every `ComponentSet` entry into one destination fact.
6. In one database transaction:
   - upsert this page's component facts;
   - advance `next_offset` by the number of outer rows returned;
   - persist the new Context and running counts.
7. Stop only when the returned outer-row count is less than 100.

Do not terminate from `Total`. Tencent documents that `Total` is cached for 24 hours and can be
lower than the actual row count. `Total` is retained only as an audit and verification value.

### Completion

The final-page transaction marks the bill day complete and clears the in-flight cursor. After
completion, query the imported partition for its distinct usage dates and refresh downstream
attribution/materializations.

Downstream refresh must never run for an in-flight partition. Pagewise writes may be visible in
the internal source ledger for a few minutes, but dashboard-serving tables remain on the last
fully completed version.

### Failure behavior

| Failure | Behavior |
| --- | --- |
| Tencent request fails | Retry the same offset; no DB change |
| Pod exits between pages | Resume from the committed `next_offset` |
| Pod exits after response but before commit | Replay one page; idempotent upsert |
| DB transaction fails | Page rows and checkpoint both roll back |
| Context expires | Retry the persisted offset without Context |
| Empty scheduled D+3 partition | Do not advance the day; retry next schedule |
| Empty explicit backfill day | Record the known empty day without creating cost facts |
| Non-target `BillDay` appears | Fail without advancing the page checkpoint |

A durable raw-response staging table or object-store spool is unnecessary unless production
metrics show repeated process-level failures that cannot be handled at page scope.

## Source component identity

The API does not expose a dedicated component-line ID. Build `source_row_hash` from stable source
identifiers:

```text
BillDay
BillId
OrderId
ResourceId
FeeBeginTime
OwnerUin
OperateUin
BusinessCode
ProductCode
ActionType
ComponentCode
ItemCode
canonical ComponentConfig
```

`ComponentSet` array position must not participate in identity because Tencent does not guarantee
stable response ordering. Within the stable outer-row identifiers above, use
`(ComponentCode, ItemCode, canonical ComponentConfig)` as the component key. If two entries in one
outer row produce the same key, fail the page as an identity collision rather than adding an
order-dependent discriminator.

Do not include these mutable payloads in identity:

```text
cost amounts
Tags
resource display name
product/component display names
FeeEndTime
PayTime
```

This lets replay update the same fact if a payload changes instead of inserting duplicate cost.
If Tencent emits two deductions with the same stable component identity, the importer deliberately
fails with an identity collision rather than using mutable settlement fields as a discriminator.
The unique destination key remains:

```text
(vendor, account_id, export_partition_date, source_row_hash)
```

Runtime metrics must compare outer-row, component-row, and unique-hash counts. A hash collision
within a source page is an error; it must not silently overwrite a distinct component.

## Destination mapping

Write one `cost_bq_export_summary_daily` fact per source component. Tencent's observed volume is
small enough for component-level storage, and resource-level identity is required for allocation
and investigation.

| Tencent source | Destination |
| --- | --- |
| literal `tencent` | `vendor` |
| `OwnerUin` | `account_id` |
| unknown until independently proven | `billing_account_id = NULL` |
| `BillDay` | `export_partition_date` |
| `DATE(FeeBeginTime)` | `usage_date` |
| `BusinessCodeName`, fallback `BusinessCode` | `service_name` |
| product/component/item codes and names | `sku_name` and source identity |
| `ActionTypeName`, fallback `ActionType` | `usage_type` |
| `RegionName`, fallback `RegionId` | `region` |
| `ResourceId`, fallback `ResourceName` | `resource_name` |
| allocation `Tags` | canonical `vendor_tags_json` |
| tags `author`, `org`, `repo` | corresponding attribution columns |
| tags `owner`, `service`, `project`, `service_exec_id` | corresponding resource dimensions |
| `PayTime` | `source_export_time` |
| `Cost` | `list_cost` |
| `RealCost` | `effective_cost`, `net_cost` |
| unavailable semantic credit | `credit_amount = NULL` |
| literal `CNY` | `currency` |

Tags remain outside identity, so the shared summary upsert must insert `currency` and extend its
`ON DUPLICATE KEY UPDATE` set to refresh `author`, `repo`, `target_branch`, and `currency` in
addition to the mutable columns it already refreshes. This makes amount-only and tag-only replays
update the existing fact instead of leaving stale attribution dimensions.

Preserve signs from Tencent. Refund and compensation transactions can be negative and must remain
additive facts rather than being rewritten as positive usage.

Do not interpret voucher, incentive, or transfer account payments as Cost Insight credits in V1.
They are payment buckets, while `RealCost` is the verified discounted bill amount. Import metrics
should still reconcile their sum to `RealCost` where Tencent supplies all buckets.

## Currency contract

Tencent is CNY; the existing GCP, AWS, Azure, and Alibaba sources are USD for this deployment.
Source facts must remain in invoice currency.

Add `currency CHAR(3) NOT NULL DEFAULT 'USD'` to the cost-bearing path used by Tencent:

- `cost_bq_export_summary_daily`;
- `cost_attribution_daily`;
- `cost_allocation_daily`;
- `cost_unmatched_resource_daily`;
- `cost_resource_serving_daily`;
- `cost_resource_serving_publication`;
- any other publication/read-model table that stores amount totals rather than only version
  pointers.

Backfill all existing facts as `USD`. Set Tencent facts to `CNY`. Carry currency through selects,
inserts, grouping keys, uniqueness keys, and amount-conservation checks so CNY and USD are never
summed before conversion. In particular:

- in `refresh_attribution_daily.py`, add currency to the select, `GROUP BY`, and every
  `dimension_hash` payload in both the direct summary refresh and TCMS-enriched summary refresh;
- in `materialize_cost_allocations.py`, carry currency in `_ATTRIBUTION_COLUMNS`, `_boundary`,
  output rows, and `_dimension_hash`, and run the native/materialized total comparisons per
  currency rather than using the current ungrouped amount sums across currencies;
- in `materialize_resource_serving.py`, carry currency through attribution/detail reads, resource
  grouping and identity keys, serving rows, and publication totals.

An implementation audit must search every `dimension_hash` constructor. The constructors in
`sync_gcp_kubernetes_workload_allocations.py` and
`sync_aws_kubernetes_workload_allocations.py` feed provider-specific USD-only staging tables and
cannot receive Tencent rows; all hash constructors on the Tencent downstream path are the refresh
and materialization sites listed above.

The dashboard uses the fixed product decision:

```text
CNY_PER_USD = 6.5
USD amount  = CNY amount / 6.5
```

Conversion occurs in the serving/API aggregation, not at ingestion:

```sql
SUM(
  CASE currency
    WHEN 'CNY' THEN net_cost / 6.5
    ELSE net_cost
  END
) AS net_cost_usd
```

Cross-cloud totals and budget comparisons use converted USD. Resource detail should retain source
amount, source currency, displayed USD amount, and the applied `6.5` rate for auditability. No FX
service or historical rate table is introduced.

## D+5 late-arrival verification

Because the account is configured by used time, D+3 cannot be assumed invoice-final. Avoid a full
rolling overlap by starting with one count probe for each imported day at D+5:

```text
Limit=1
NeedRecordNum=1
BeginTime/EndTime = exact verified BillDay
```

Compare `Total` with the completed import's outer-row count:

- equal: mark the day verified without reading further pages or writing cost rows;
- greater: late transactions exist; replay that day pagewise and upsert the new source identities;
- smaller: wait past the 24-hour cache horizon and retry the probe on the next schedule; if it is
  still smaller, perform one read-only pagewise scan that ignores `Total` and compare its actual
  row count, order-independent identity fingerprint, amount totals, and latest `PayTime` with the
  completion evidence.

If that read-only scan matches the completion evidence, record the probe as a stale `Total` and
mark the day verified without writing cost rows. API totals are compared to DB-backed completion
evidence with a `1e-9` Decimal tolerance, matching the stored amount precision. If it confirms
fewer or changed records, or if month-close finds an amount-only mismatch, stop automatic repair
and require an explicit partition replacement plan.

This works because Tencent documents L3 as one record per deduction; refunds, adjustments, and
re-settlement normally add transactions rather than editing historical usage totals in place.
The residual risk of an in-place amount mutation is covered by month-close reconciliation.

## Month-close reconciliation

After 19:00 Beijing time on the first day of the following month, call
`DescribeBillSummaryForOrganization` once for the closed month. Read-only validation confirmed
that a single business-grouped request returns organization summary totals.

Record the source `TotalCost`, but reconcile the billed amount only:

```text
RealTotalCost  ↔ SUM(net_cost)
```

`DescribeBillDetailForOrganization` supplies `ComponentSet.Cost` and `RealCost`,
but no detail-level `TotalCost`. Production validation showed that the component
`Cost` sum need not equal `DescribeBillSummaryForOrganization.TotalCost`, while
component `RealCost` exactly matched `RealTotalCost`. `TotalCost` is therefore
observational only; `RealTotalCost` is the month-close correctness gate.

Use `Decimal`, not binary floating point. For each month, coverage starts from the scheduled job's
first completed day when it predates that month, that month's earliest stored Tencent
`export_partition_date`, or a completed explicit range that spans the whole month. The range state
makes a full backfill eligible even if its opening bill day has no rows. A month with neither source
rows nor applicable scheduled/range coverage is recorded as `partial-coverage` with a null
`coverage_start`; skip any month whose coverage begins after its first day. If `Ready=0`, record a
non-fatal `unready` result so a later schedule retries it. A `RealTotalCost` mismatch fails once and
is retried only after an explicit repair changes the stored monthly net total, except that an
intervening unready summary is probed again on the next schedule. If available totals match,
perform no detail reads or writes. Do not make a full monthly detail scan part of the normal
schedule.

## Relationship to existing vendor collectors

| Vendor | Source partition behavior | Normal import behavior |
| --- | --- | --- |
| GCP | immutable-ish daily export partitions | append/upsert new partitions, configured overlap |
| Alibaba | daily `partition_date` shards | append/upsert new partitions |
| AWS legacy CUR | mutable monthly files | monthly overlap/upsert |
| Tencent | daily `BillDay`, but by-used-time late arrivals are possible | D+3 pagewise append/upsert plus a D+5 count probe; bounded confirmation only on mismatch |

Tencent aligns with GCP's three-day dashboard freshness but does not copy GCP's full
recent-partition overlap. The D+5 count probe is the provider-specific correction guard.

## Configuration and credentials

Use the official Tencent Cloud Python SDK, not a `tccli` subprocess. Inject credentials from a
Kubernetes Secret using the SDK's standard environment contract. Never log credentials, request
signatures, download URLs, or complete billing payloads.

Proposed settings:

```text
COST_INSIGHT_TENCENT_ACCOUNT_ID=100050658403
COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY=YYYY-MM-DD
COST_INSIGHT_TENCENT_IMPORT_LAG_DAYS=3
COST_INSIGHT_TENCENT_VERIFY_LAG_DAYS=5
COST_INSIGHT_TENCENT_PAGE_SIZE=100
```

Validate page size as `1..100`. `account_id` must match every returned `OwnerUin`; a mismatch fails
the page before checkpoint advancement. The `source_available_from` value in the Tencent source
seed must equal the configured `COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY`. Leave `billing_account_id`
null until the organization payer identity is independently available from an authoritative source.

## CLI and code boundaries

Add one source adapter and one job:

```text
src/cost_insight/sources/tencent_billing.py
src/cost_insight/jobs/sync_tencent_billing_summary.py
```

Extend:

```text
src/cost_insight/common/config.py
src/cost_insight/jobs/cli.py
src/cost_insight/jobs/state_store.py
src/cost_insight/jobs/sync_gcp_billing_summary.py
src/cost_insight/jobs/refresh_attribution_daily.py
src/cost_insight/jobs/materialize_cost_allocations.py
src/cost_insight/jobs/materialize_resource_serving.py
sql/<next>_add_cost_currency.sql
pyproject.toml
```

Normal command:

```bash
cost-insight sync-tencent-billing-summary
```

Operator-only options may select an explicit bill-day range for dry-run, backfill, or repair. A
replacement flag must never be enabled by the scheduled command.

Reuse the existing cost-source registry, attribution refresh, job-state table, and materialization
jobs. Extend the shared summary upsert and state-store helper as specified above rather than
creating Tencent-only copies. Do not introduce a generic provider framework for one Tencent
adapter.

## Observability

Log structured per-page and per-day metrics without billing payloads:

```text
bill_day
page_offset
outer_rows
component_rows
rows_inserted_or_updated
request_attempts
source_real_cost_cny
last_pay_time
page_duration_seconds
day_completion_status
```

Persist completion evidence in the watermark:

```text
outer_row_count
component_row_count
source list/effective/net totals
last PayTime
completion timestamp
D+5 verification status and observed Total
first_completed_bill_day
reconciled_months, including partial-coverage, unready, matched-real-cost-only, legacy matched, and mismatch states
```

Alert when:

- an in-flight day is not completed by the next schedule;
- D+5 `Total` increases or a confirmation scan finds fewer or changed records;
- a month-close summary remains unready or requires an explicit repair;
- page identity collisions occur;
- OwnerUin differs from the configured source;
- month-close net total does not reconcile;
- Tencent API throttling/retry exhaustion occurs.

## Tests

Unit and SQLite integration tests must cover:

1. exact-day request construction and Beijing date handling;
2. `Limit=100`, Offset, and Context pagination;
3. multiple components in one outer row;
4. stable source identity under reordered `ComponentSet` responses and page replay idempotency;
5. amount and tag changes refreshing `author`, `org`, `repo`, `target_branch`, and the other
   mutable columns on the same identity;
6. API failure leaving the page checkpoint unchanged;
7. DB failure rolling back both page rows and checkpoint, with page checkpoints preserving the
   running lifecycle status;
8. restart from the committed offset;
9. expired Context fallback to the same offset;
10. target-day and OwnerUin validation;
11. empty scheduled D+3 data not advancing completion, while an explicit backfill can complete a known empty day;
12. D+5 equal/increased/decreased `Total` behavior, including read-only confirmation after a
    persistent smaller value;
13. completed-only downstream refresh;
14. separate hashes/groups for otherwise identical CNY and USD rows, plus `CNY / 6.5` USD display
    conversion;
15. month-close reconciliation using exact decimal arithmetic;
16. explicit repair never running from the scheduled command.

Use redacted API fixtures. Tests must not require Tencent credentials.

## Rollout and gates

### Gate 1: temporal D+3 validation

A full D+3 fingerprint was captured for `BillDay=2026-09-13` on 2026-09-16:

```text
outer rows: 4,456
component rows: 4,456
RealCost: CNY 256.85000000
latest PayTime: 2026-09-14 08:20:52
```

Re-fetch the same day on D+4/D+5 and compare row count, order-independent canonical hash, amount
totals, and latest PayTime. This gate decides whether production starts with D+3 or temporarily
uses D+5. It does not block writing or reviewing the importer design.

### Gate 2: supernode bill-tag canary

Confirm that the existing `eks-6ikqrb5l` canary bill row contains the expected `author`, `org`, and
`repo` tags. This gate blocks claiming end-to-end Pod-label attribution and blocks production Prow
mutation rollout. It does not block importing general Tencent resource cost.

### Deployment phases

1. Apply currency schema and Tencent source seed migrations; the source remains disabled until the credential and dry-run pass. Verify existing USD totals are unchanged.
2. Run one explicit Tencent bill day in dry-run and reconcile source counts and amounts.
3. Run pagewise import for that day and verify idempotent replay.
4. Enable the CronJob with `concurrencyPolicy: Forbid` after Gate 1 selects the initial lag.
5. Enable Prow supernode tag mutation only after Gate 2.
6. Backfill only the approved date range, oldest bill day first.
7. Verify the first D+5 count probe and first closed-month summary reconciliation.

## Acceptance criteria

- A normal schedule reads each new D+3 bill day once, one API page at a time.
- Each page and its checkpoint commit atomically.
- Restart resumes from the last committed page and page replay cannot duplicate cost.
- Completed stable days are not fully reread unless a D+5 or month-close check finds a discrepancy.
- No normal path deletes or replaces a month or day.
- Tencent billed (`RealTotalCost`) totals reconcile in CNY.
- Existing vendor totals remain USD.
- No dimension hash, grouping, conservation check, or publication total combines currencies before
  conversion.
- Dashboard cross-cloud values use the fixed `6.5 CNY/USD` conversion and identify the applied rate.
- Attribution and serving refresh only after the source bill day is complete.
- Missing or historical tags remain visible as unmatched rather than being backfilled from current
  resource state.
