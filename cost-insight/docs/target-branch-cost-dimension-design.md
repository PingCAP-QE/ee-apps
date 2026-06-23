# Target Branch Cost Dimension Design

## Context

Cost Insight currently attributes CI cost by source account, service, SKU,
author, org, repo, roster group, and manager. The dashboard needs one more CI
dimension: target branch.

Prow already places the target branch on Kubernetes pods as:

```text
prow.k8s.io/refs.base_ref
```

In GCP Cloud Billing detailed export, Kubernetes labels are exposed with the
`k8s-label/` prefix. The normalized billing label key is:

```text
k8s-label/prow.k8s.io/refs.base_ref
```

The cost pipeline should map that source label to a product-facing
`target_branch` column instead of leaking the Prow label key into dashboard
queries.

## Source Validation

Validation was run against:

```text
gcp-digital-bi.gcp_billing_detailed.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6
```

for project:

```text
pingcap-testing-account
```

Recent billing export partitions include these Prow labels:

| Billing label key | Rows in recent partitions | Distinct values |
| --- | ---: | ---: |
| `k8s-label/prow.k8s.io/refs.org` | 682,818 | 6 |
| `k8s-label/prow.k8s.io/refs.base_ref` | 682,818 | 44 |
| `k8s-label/prow.k8s.io/refs.repo` | 682,818 | 23 |
| `k8s-label/prow.k8s.io/refs.pull` | 78,441 | 1,176 |
| `k8s-label/prow.k8s.io/refs.author` | 77,206 | 114 |

Sample `refs.base_ref` values include release branches such as:

```text
release-8.5-20260618-v8.5.5
release-nextgen-20251011
release-9.0-beta.2
release-8.1
```

Recent complete usage days show the label is not universal, but it covers a
material CI/Prow cost slice. For example:

| Usage date | Rows with target branch | Row coverage | Net cost with target branch | Net cost coverage |
| --- | ---: | ---: | ---: | ---: |
| 2026-06-22 | 24,621 | 9.02% | 182.09 | 21.53% |
| 2026-06-18 | 40,414 | 10.27% | 275.83 | 24.66% |
| 2026-06-16 | 59,293 | 10.43% | 367.16 | 22.17% |
| 2026-06-11 | 76,129 | 11.91% | 542.39 | 27.63% |

## Goals

- Add `target_branch` as a first-class cost dimension.
- Keep the dashboard independent from raw Prow/Kubernetes label names.
- Preserve branch granularity through summary import, attribution refresh, and
  dashboard APIs.
- Keep existing cost totals unchanged when no branch filter is applied.
- Make the change idempotent and backfillable.

## Non-Goals

- Do not add a duplicate Kubernetes pod label. The existing Prow label is the
  source of truth.
- Do not infer target branch from repo names, PR numbers, or job names in the
  cost pipeline.
- Do not add branch support for AWS until an equivalent source dimension exists.
- Do not require resource-level billing reads for normal dashboard summary
  pages.

## Data Model Changes

Add nullable `target_branch VARCHAR(255)` to the Cost Insight fact tables that
carry CI dimensions:

- `cost_raw_details`
- `cost_bq_export_summary_daily`
- `cost_unmatched_resource_daily`
- `cost_attribution_daily`

Only the dashboard-facing attribution table needs a branch index for this
change. The raw, BigQuery summary, and unmatched-resource tables only carry the
dimension through ingestion/refine jobs today; avoid extra write and migration
cost there until a real query path needs it.

```sql
ALTER TABLE cost_raw_details
  ADD COLUMN target_branch VARCHAR(255) NULL;

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN target_branch VARCHAR(255) NULL;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN target_branch VARCHAR(255) NULL;

ALTER TABLE cost_attribution_daily
  ADD COLUMN target_branch VARCHAR(255) NULL,
  ADD INDEX idx_cost_attribution_daily_branch (usage_date, target_branch);
```

If production query plans later show account-summary branch filters are slow, add
a more targeted compound index based on the observed SQL shape instead of
preemptively indexing every intermediate table.

## BigQuery Normalization

Add a `target_branch` projection to GCP billing queries:

```sql
ARRAY(
  SELECT label.value
  FROM UNNEST(labels) AS label
  WHERE label.key IN (
    'k8s-label/prow.k8s.io/refs.base_ref',
    'prow.k8s.io/refs.base_ref'
  )
  LIMIT 1
)[SAFE_OFFSET(0)] AS target_branch
```

The unprefixed fallback is included for defensive compatibility, but current
validated billing export rows use the `k8s-label/` form.

Add `target_branch` to `GROUP BY` in:

- `build_gcp_billing_query`
- `build_gcp_billing_summary_query`
- `build_gcp_unmatched_resource_query`

This is necessary because different branches can share the same day, repo,
author, service, and SKU.

## Hashing And Idempotency

Add `target_branch` to all dimension hashes where the source table stores
branch:

- `sync_gcp_billing_export.HASH_FIELDS`
- `sync_gcp_billing_summary.HASH_FIELDS`
- `sync_gcp_unmatched_resources.HASH_FIELDS`
- attribution `dimension_hash` SQL for both raw and summary refresh paths

This prevents rows from different target branches from being merged or
overwritten during upsert.

Amount fields and `source_export_time` should remain outside the hashes, as
they do today.

## Existing DB Data

Existing TiDB rows cannot be updated in place to recover target branch
granularity. The current summary and attribution rows were grouped without
`target_branch`, so one stored row may contain cost from several branches. After
schema migration, those existing rows can only be marked as
`target_branch = NULL`; they cannot be split correctly from TiDB alone.

To add this dimension for historical data, rebuild the selected window from the
source BigQuery billing export:

1. Add nullable `target_branch` columns.
2. Deploy collectors that include `target_branch` in query grouping and hashes.
3. For each backfill export partition, delete old branchless summary rows for
   that partition before reimporting branch-aware rows. Use
   `sync-gcp-billing-summary --replace-existing-partitions` for this.
4. Re-run `sync-gcp-billing-summary` for the selected export partitions.
5. Re-run `refresh-cost-attribution-from-summary` for the touched usage dates.

The delete step is important. Because the new hash includes `target_branch`,
branch-aware rows will have new `source_row_hash` values and will not overwrite
the old branchless summary rows. If both sets remain, total cost will be double
counted.

The importer replacement mode should delete by exact import scope before
re-sync:

```sql
DELETE FROM cost_bq_export_summary_daily
WHERE vendor = :vendor
  AND account_id = :account_id
  AND export_partition_date BETWEEN :export_partition_start AND :export_partition_end;
```

Then refresh attribution for the resulting touched usage dates. Attribution
refresh already deletes and rebuilds by `usage_date`, `vendor`, and
`account_id`, so it is naturally compatible with the new branch dimension once
the summary table is correct.

For future data, the normal incremental summary importer will populate
`target_branch` without a special job.

If raw details are also re-synced for a historical window, use
`sync-gcp-billing-export --replace-existing-dates` for that explicit usage date
range. The raw hash also includes `target_branch`, so a historical raw re-sync
without replacement can leave old branchless rows next to new branch-aware rows.

## Pipeline Flow

1. GCP billing export query reads `k8s-label/prow.k8s.io/refs.base_ref`.
2. Collector normalizes it into `target_branch`.
3. Summary/raw/unmatched upserts persist the column.
4. Attribution refresh copies `target_branch` from source rows to
   `cost_attribution_daily`.
5. Attribution grouping and `dimension_hash` include `target_branch`.
6. Dashboard cost queries can filter and group by `target_branch` directly.

## Dashboard Changes

Update `CommonFilters` usage for cost views so branch is no longer discarded in
cost-specific normalization.

Add branch filtering to cost query predicates:

```sql
AND c.target_branch = :branch
```

Unlike CI build queries, cost tables do not have a `base_ref` fallback column.
The cost pipeline should store the normalized branch directly as
`target_branch`.

Potential UI/API additions:

- Preserve `branch` in cost page filters.
- Allow `group_by=target_branch` on the cost stack endpoint.
- Add a cost branch list endpoint if the UI needs branch selector options from
  cost data instead of build data.

## Backfill Strategy

For future data, normal incremental jobs will populate `target_branch`.

For historical data, run a bounded BigQuery backfill for the desired usage or
export partition window:

1. Deploy schema changes.
2. Deploy collectors with `target_branch` normalization.
3. Delete old summary rows for selected export partitions.
4. Re-run `sync-gcp-billing-summary --replace-existing-partitions` for selected
   export partitions, or run a dedicated backfill command if the window is large.
5. Re-run `refresh-cost-attribution-from-summary` for touched usage dates.
6. Optionally re-run unmatched-resource import for the latest investigation
   window.

If historical branch data is required only for dashboard summaries, backfill
`cost_bq_export_summary_daily` and `cost_attribution_daily`; do not backfill
resource-level raw details unless investigation pages need it.

For the June-only rollout requested in this change, the intended operational
shape is:

```bash
cost-insight sync-gcp-billing-summary \
  --export-partition-start 2026-06-01 \
  --export-partition-end 2026-06-30 \
  --replace-existing-partitions

cost-insight refresh-cost-attribution-from-summary \
  --start-date 2026-06-01 \
  --end-date 2026-06-30 \
  --split-by-day
```

For a more conservative rollout, split the export partition range manually into
daily `sync-gcp-billing-summary --replace-existing-partitions` commands.

Only re-sync raw details if the raw-detail path is needed for the same June
window:

```bash
cost-insight sync-gcp-billing-export \
  --start-date 2026-06-01 \
  --end-date 2026-06-30 \
  --replace-existing-dates \
  --split-by-day
```

## Validation Plan

- Unit-test BigQuery query builders include `target_branch` in `SELECT` and
  `GROUP BY`.
- Unit-test normalizers copy `target_branch` and include it in row hashes.
- Unit-test attribution refresh keeps two otherwise-identical rows separate
  when `target_branch` differs.
- Unit-test cost API branch filter and `group_by=target_branch`.
- Run a BigQuery dry run or limited sync for one recent day.
- Compare total cost before and after with no branch filter; totals should
  match.
- Query branch-filtered dashboard data for a known recent branch such as a
  `release-*` value and verify non-zero results.

## Rollout Notes

- Add nullable columns first; no API behavior changes should depend on
  backfill being complete.
- Deploy collector changes before enabling dashboard branch UI.
- Monitor row counts in summary and attribution tables because branch
  cardinality will increase fact row counts.
- Treat missing `target_branch` as normal unlabeled cost, not as an ingestion
  failure.
