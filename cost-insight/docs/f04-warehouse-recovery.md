# AWS F04 warehouse recovery

## Incident and scope

On 2026-11-09, both the summary ledger and Dashboard attribution for AWS account
`380838443567` (`tidb-cloud-prod-us-west-2-f04`) stopped at usage date 2026-10-26.
The dedicated legacy job still reported success after importing zero rows. Its
registered source was the old external table
`gcp-digital-bi.aws_prod_billing.aws_prod_billing_data`, not the current warehouse.

The source is **not the whole AWS account**. Keep every boundary below:

- account: `380838443567`;
- nested resource tag `user_usedby = prod-us-west-2-f04`;
- currency: USD;
- charge types: `Usage` and `SavingsPlanCoveredUsage`;
- profile: `aws_tidb_cloud_f04_v1`, earliest available usage 2026-08-01.

An exact-boundary AmazonEC2-only warehouse query found rows on all seven days
2026-11-02..08 (USD 761.4331845157 list cost). This proves missing imported
consumption, **not** the complete F04 total. Never import only that subset or use
an account-wide mart/watermark as proof of F04 completeness.

## Application change

`sync-warehouse-billing-summary` recognizes only the exact F04 account with its
existing profile and an explicit warehouse `source_table`. It must run separately
from ordinary AWS accounts. Discovery, raw controls and the existing F04 summary
builder all retain the same tenant/charge boundaries. Summary rows keep the
existing **direct** hash semantics: the routing profile is not written into
`summary.source_schema_version` (that field selects split-cost normalization).

The reader splits F04 queries by **billing month**, not by product. Every query
reads the complete F04 product/SKU scope for its month and usage window. No
expensive product or failed month may be skipped. All required reads, raw list-cost
controls, source snapshot checks, and daily coverage validation finish before
any summary replacement. USD is required; missing source days are errors, not
authoritative zero-cost deletions.

The shared runner retains account + billing-partition + usage-day replacement,
canonical hash collision aggregation, untouched sibling-partition checks,
sequential daily attribution, and active native-publication monetary verification.
It rejects a local backlog larger than the window: repair missing history in
order before enabling the ordinary trailing-five-day schedule.

F04 has its own durable failure latch, `sync_aws_f04_warehouse_daily`; it cannot
bypass a failed/running F04 run or block the unrelated ordinary AWS warehouse job.
The legacy F04 writer now fails on zero rows **before any empty partition
replacement or success watermark**, instead of reporting a successful empty import.
Read-only dry runs still report empty results without mutating job state.

### Explicit scan budget

Ordinary AWS/Azure reads retain **2 GiB per query**. Complete F04 raw detail needs a
separate **fixed 32 GiB per-query cap**, with dry-run preflight, bounded timeouts,
actual-byte validation and source snapshot checks. This is not an unlimited retry
or a global budget increase. The deployment review must explicitly accept this
F04-specific query budget before enabling the route.

Read-only dry-run estimates at investigation time:

| Complete F04 summary usage window | Billing month | Estimated bytes |
| --- | --- | ---: |
| 2026-10-27..31 | October | 28,982,844,978 (~26.99 GiB) |
| 2026-11-04..08 | November | 9,205,481,573 (~8.57 GiB) |

These are estimates, not executed high-budget reads, and can grow with source
volume. A crossing-month discovery/window issues one bounded query per month;
it does not combine two months into one oversized read. Any unknown/over-budget
estimate or actual read fails closed. Shorter usage dates do not necessarily
reduce scanning because the physical partition key is billing month. Each query
has its own cap; total run cost also includes discovery and summary queries.

F04 list cost is unrounded Usage/SP unblended cost. Existing F04 effective/net
semantics remain unblended cost (blended fallback), rounded per normalized SQL
group, with zero credit. Raw-to-normalized list cost and normalized-to-published
four-field conservation are checked; do not compare a rounded sum to raw daily
cost as if the two were the same measure.

## Deployment gate and sequence

This document and SQL file do **not** execute a migration, deploy an image, or
backfill any facts. An application PR alone is not a recovered Dashboard.

1. Release the reviewed application image. Verify the actual successful workflow
   tag and both platform manifests, then pin its immutable digest in OPS.
2. Before switching the dedicated F04 CronJob, ensure no legacy F04 writer is
   active. Stop scheduling the old route for the cutover; do not run old and new
   writers concurrently. Keep all unrelated CronJobs/source profiles untouched.
3. Apply the guarded `sql/030_move_aws_f04_to_warehouse.sql`, then verify exactly
   one active source has `source_table =
   warehouse-f80fbbe8e8.raw_aws_billing.cur_line_items` **and the original F04
   profile**. Retain billing account, display name, category and available-from.
   Do not relabel it as an ordinary account. The old flat-CUR importer is not a
   compatible route after this source change.
4. Set the dedicated worker to the released image, command below, and KSA
   `cost-insight-gcp-billing` with direct Workload Identity. Configure
   `COST_INSIGHT_BILLING_BQ_PROJECT=warehouse-f80fbbe8e8`,
   `COST_INSIGHT_BILLING_BQ_LOCATION=us-west1`,
   `COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT=""`, and
   `COST_INSIGHT_TCMS_ALLOCATION_TABLE=tcms_cost.resource_allocation`.
   Reuse the existing DB Secret; no new keys or IAM grants are needed. The old
   shared `ci-dashboard` KSA cannot impersonate the warehouse billing identity.
5. Run a released-image read-only preflight. Check full F04 normalized SKU/tag
   dimensions and controls, payer, currency, all requested days, local preserved
   partitions, and query budgets. Older partial product comparisons are not
   substitutes for the complete query.
6. Recover missing usage from **2026-10-27** through the latest actually available
   source date in a durable in-GKE coordinator. Use sequential **1..5 inclusive
   day** windows, `backoffLimit: 0`, and stop on the first failed/uncertain batch.
   Initial example windows: 10/27..31, 11/01..05, 11/06..08 (rediscover the final
   end date at execution). Every window imports, attributes and republishes each
   day; do not follow it with an unscoped generic refresh.
7. Verify complete summary -> attribution -> active native publication amounts,
   exact F04 tenant boundary, and source continuity. Then enable the dedicated
   daily command, preserving a non-overlapping schedule and `concurrencyPolicy:
   Forbid`. A code/OPS PR merge is not evidence that the historical gap is fixed.

Recurring command (latest actual usage date, trailing five complete days):

```sh
cost-insight sync-warehouse-billing-summary \
  --vendor aws --account-id 380838443567 --dry-run
```

Historical batch command (omit `--dry-run` only in an approved GKE child Job):

```sh
cost-insight sync-warehouse-billing-summary \
  --vendor aws --account-id 380838443567 \
  --start-date 2026-10-27 --end-date 2026-10-31 --dry-run
```

Explicit dates are F04-only, require both bounds, permit at most five inclusive
calendar days, must end before UTC today, and are not silently shifted to a
recent window. The command already refreshes attribution/publication by day;
there is no extra `--split-by-day` flag for this warehouse command.

Never pair the legacy `--replace-existing-partitions` with a five-day repair: it
deletes entire billing-month partitions. Never clear the F04 latch automatically
on failure. Inspect its exact window/day and any committed effects, then use a
reviewed, cluster-owned recovery plan. Rollback means holding the affected F04
writer, not resuming an empty/stale legacy source or deleting validated facts.
