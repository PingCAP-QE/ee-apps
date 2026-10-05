# Cost Breakdown Resource Drilldown

Status: Proposed
Date: 2026-09-02
Owners: Cost Insight (projection writer), CI Dashboard (read API and UI)

## Problem and evidence

The Cost page's **Resource breakdown** is intended to explain an Owner slice
selected from **Cost breakdown (list cost)**. It currently fails that contract:

1. `ci-dashboard` returns an empty `pending_dates` response when *any* expected
   source/date lacks a `cost_resource_serving_publication` row. The UI then
   shows “Resource details are being materialized. Please retry on the next
   refresh.” rather than a table.
2. Attribution and resource-detail imports explicitly delete those publication
   rows, but `materialize-resource-serving` is a separate, manually invoked CLI
   command. No write-path code rematerializes the invalidated dates. Normal
   upstream refreshes therefore leave the panel pending until a separate job
   happens to run.
3. The serving API is a Top-10 list, so it is not a complete resource
   drilldown. It exposes only `resource_name`; it cannot separately display a
   provider resource identifier (for example, AWS `i-…`) and a usable name
   (for example, an S3 bucket name).
4. The serving projection currently sums the source `list_cost` directly,
   whereas Cost breakdown uses the dashboard billing-report list-cost rule
   (GCP `Compute Flexible Committed Use Discounts%` contributes zero). The two
   panels can therefore disagree.

The original raw-ledger query must **not** be restored as a fallback: its
nullable/JSON joins are what caused the no-owner request's memory cancellation
and gateway 504. The existing published serving projection remains the bounded
read model.

## Scope and non-goals

This change covers the existing Owner drilldown only: clicking a concrete Owner
slice in **Cost breakdown (list cost)** opens that Owner's resources for the
same selected date range and cost source. `Others` remains non-interactive.

It adds a complete, paginated resource list and the resource fields necessary
for AWS/GCP console lookup. It does not add lifecycle/inventory collection,
Team drilldowns, a resource detail page, or a new request-time join to raw
billing tables.

## User-visible contract

The table becomes **Resource breakdown: <Owner>** and is a paginated list,
not “Top 10 billable resource rows.” Each row is one resource aggregated across
all selected usage dates and has these columns:

| Column | Contract |
| --- | --- |
| Resource ID | Provider/billing identifier. AWS EC2 shows the CUR `line_item_resource_id` such as `i-0123…`; GCP shows the provider global resource name when supplied. `--` means the billing export did not supply an identifier. |
| Name | Console-searchable resource name when supplied. For AWS, use the resource ID when it is the only name; for S3 prefer the CUR resource ID/bucket name, then the provider `Name` tag, then the billing description. A description is visibly just a billing fallback, never a fabricated bucket identity. |
| Service | All distinct billing services contributing to the selected resource's filtered serving rows, joined by `GROUP_CONCAT(DISTINCT s.service_name ORDER BY s.service_name)`. With a `service_name` filter, it therefore shows only the filtered service; without one, it shows the resource's full service set. |
| List cost | Sum of the Dashboard billing-report list-cost measure over the selected dates. It is the default descending sort. |
| Duration | Sum of available `usage_seconds` over the selected dates. The reader uses `SUM(s.usage_seconds)`, never `SUM(COALESCE(s.usage_seconds, 0))`, so it returns `NULL`/`--` only when every contributing row is NULL; a displayed `0` is a known zero. It is not a resource lifetime. |
| Labels | Deterministic representative provider/billing labels from the contributing serving row with greatest absolute `list_cost`. Across days, the reader ranks the filtered rows by `ABS(s.list_cost) DESC, s.usage_date ASC, s.resource_key ASC, COALESCE(s.target_branch, '') ASC, COALESCE(s.representative_labels_json, '') ASC` and selects rank one; it must not use `MIN(representative_labels_json)`. The existing variation count remains available for a later details view but is not silently used for matching. |

A concrete resource must have a stable `resource_id` when the provider export
provides one. For identifiers absent from the billing export, the cost row stays
visible with its name, labels, and explicit `Resource ID = --`; the UI must not
claim that it can be found in the provider console.

The first page contains 50 rows (maximum 100). `Load more` obtains the next
page without changing the Owner/date/source scope. This removes the Top-10
truncation while keeping each response bounded.

## Cost and aggregation invariants

For a selected Owner and the filters sent by the Cost page:

```text
sum(all pages' resource list_cost)
= Cost breakdown's list_cost for that Owner
```

The equality uses the same source/date filters, owner normalization
(`NULL`/empty owner is `(no owner)`), branch behavior, and billing-report
list-cost expression. A `service_name` filter intentionally narrows the
resource list and is not required to equal the Owner total. Remove the current
silent 31-day reader clamp: the endpoint must either aggregate the entire
selected Cost-page range or return `pending_dates` for dates that are not
published. It must never replace the requested start date and present a
shorter period as the chart's drilldown. The serving projection—not a raw
ledger join—is still the only read source, so the query remains bounded by the
selected published source/date windows.

The serving materializer must apply the dashboard measure before distributing
the source amount:

```text
CASE
  WHEN vendor = 'gcp'
   AND sku_name LIKE 'Compute Flexible Committed Use Discounts%'
  THEN 0
  ELSE list_cost
END
```

Rows whose selected-period aggregate list cost is zero are excluded from the
resource list. Negative adjustments remain included. Thus discount rows cannot
appear as zero-cost resource noise or make the drilldown disagree with the
chart.

Concrete `resource_detail` rows use the existing provider/account-scoped
`resource_group_key`, regenerated from:

```text
vendor + account_id + canonical resource_id (when present)
otherwise vendor + account_id + resource_name + parent resource
```

This key deliberately excludes usage date, service, SKU, and labels. It makes
one concrete resource remain one row across several days and services.
`list_cost` and available `usage_seconds` are summed; Service and Labels are
chosen deterministically and do not change cost identity.

Fallback rows remain explicit `attribution_fallback` resources and conserve
unresolved source cost. Because `cost_attribution_daily` has no resource ID or
parent resource, their `resource_group_key` remains
`vendor + account_id + source_fact_hash + attribution_fallback` (where the
materializer loads `dimension_hash` as `source_fact_hash`), never
`resource_name + ''`. This preserves source-fact cardinality, including facts
with a NULL resource name, rather than collapsing unrelated unresolved cost
into one apparent resource. Fallbacks have `resource_id = NULL` and are not
mislabeled as provider resources.

## Data changes

Cost Insight owns the shared schema. Add an additive migration after `019`:

```sql
ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN resource_id VARCHAR(1024) NULL AFTER resource_name;

ALTER TABLE cost_resource_serving_daily
  ADD COLUMN resource_id VARCHAR(1024) NULL AFTER resource_name;
```

The raw resource ledger retains both fields. Source projections populate them
as follows:

- **AWS CUR and AWS split CUR:** `resource_id` is the exact non-empty provider
  resource ID (`line_item_resource_id`, with the split child/parent identity
  retained where appropriate). `resource_name` is resource ID when available;
  for S3 it may fall back in order to a `Name` tag and then a billing
  description. All provider tags are retained as labels, not just the current
  ownership-routing tags. The projection also emits the existing compact
  `cluster`/`shared_pool` JSON separately as `summary_vendor_tags_json`; only
  that compact value is passed into the canonical summary-lineage hash. Full
  labels are persisted only as resource metadata, so adding visible labels
  cannot break the exact resource-to-summary join.
- **GCP detailed billing export:** `resource_id` is
  `resource.global_name` when available, otherwise `resource.name`.
  `resource_name` preserves the existing displayable resource value so GKE
  summary identity is unchanged.

`resource_id` is presentation and serving-group identity metadata. The existing
raw source-row hash remains based on the established billing-row identity so an
identifier enrichment updates the existing raw row rather than leaving an old
identifier-less duplicate. The rollout's bounded date reimport removes any
legacy rows that cannot be updated in place.

This does not add a second period-aggregate table. Each page aggregates only
published serving rows for the Owner/source/date predicate through
`idx_resource_serving_owner_date`, fetches `page_size + 1`, and uses a
validated keyset cursor. It performs no raw-ledger join, total-row count, or
per-page materialization/validation; those would add cost without improving the
user's investigation workflow.

`cost_resource_serving_daily` also retains source `project` and `group_id`.
A resource's physical serving key includes those attribution scopes, while its
response `resource_group_key` stays provider-resource based. This lets the
Dashboard filter the same resource to a selected Project or Engineering Group
Team before aggregating its displayed row; resource costs cannot leak across
those selected scopes. The Project column is introduced by migration `021` and
requires the standard bounded rematerialization before Project drilldown is
available.

The materializer copies `resource_id` to every concrete serving row. Fallbacks
have `resource_id = NULL`. It recomputes group/resource keys using the rule
above and writes a new staged version before moving the daily publication
pointer, so no request observes an incomplete version.

## Publication and refresh behavior

The current invalidation safety property is retained: a serving publication is
removed when its attribution or resource-detail input changes, and the reader
never aggregates a mix of published and unpublished dates.

The missing piece is automatic, source-scoped rematerialization. Extend
`run_materialize_resource_serving` with paired `vendor` and `account_id`
filters (both supplied together or neither). When supplied, `_source_windows`
materializes only that source's existing attribution dates, plus empty dates
covered by a successful attribution-refresh watermark. A resource import alone
must not publish an empty date before attribution has succeeded; that date stays
pending. The standalone CLI without those filters retains its unscoped
backfill/repair behavior. After a successful, non-dry-run mutation of a bounded
`(vendor, account_id, usage_date)` range, both of these paths invoke
`run_materialize_resource_serving` with that exact source and date range, only
after their write transaction commits:

1. `run_refresh_cost_attribution_from_summary` after its attribution
   transaction commits;
2. `run_sync_gcp_unmatched_resources` and
   `run_sync_aws_unmatched_resources` after their resource rows commit.

This is idempotent. Whichever upstream path completes last republishes the
current combination of attribution and resource detail. A normal daily refresh
therefore heals its own invalidated publication; it does not depend on an
unrelated manual materializer CronJob. The standalone CLI remains available for
backfill and repair.

Daily source windows stage and publish independently. If materialization fails
mid-run, windows already processed remain published and only the unprocessed,
invalidated windows remain pending; the upstream job is marked failed.
`pending_dates` detects those missing windows and returns no aggregate, so the
Dashboard never exposes a partial table. The UI says which dates are unavailable
and links the operator to refresh the resource-serving job; it no longer
presents normal refresh lag as an expected user action.

## API and UI

Keep `GET /api/v1/pages/cost-unmatched-resources` for compatibility, but make
its product name and response a resource drilldown. Existing `owner`,
`service_name`, and `sort_by=list_cost|duration` parameters remain. A selected
Cost breakdown Team or Project passes `scope_dimension=team|project` and
`scope_value`; an Owner selected beneath a Team passes both the Owner and Team
scope. Add:

```text
page_size: 1..100 (default 50)
cursor: opaque keyset cursor, optional
```

The endpoint reads only the active native serving version joined to its daily
publication pointers. It must not read `cost_unmatched_resource_daily` or
`cost_attribution_daily` on the request path.

An item's opaque `resource_key` is the response alias for the aggregated
`resource_group_key`; it is not the per-service, per-day physical
`resource_key` stored in the serving table.

An item has at least:

```json
{
  "resource_key": "…",
  "resource_id": "i-0123456789abcdef0",
  "resource_name": "i-0123456789abcdef0",
  "service_name": "AmazonEC2",
  "labels": "cluster=…",
  "list_cost": 123.45,
  "usage_seconds": 259200.0,
  "resource_data_source": "resource_detail",
  "resource_detail_cost": 123.45
}
```

`meta.next_cursor` is present only when another page exists. Its sort order is
`list_cost DESC, usage_seconds IS NULL ASC, usage_seconds DESC,
resource_group_key ASC` for list-cost sort, or `usage_seconds IS NULL ASC,
usage_seconds DESC, list_cost DESC, resource_group_key ASC` for duration sort.
The cursor contains exactly the selected ordering values, including the
duration-null flag, and the server validates its shape; it is not an SQL
fragment. A resource with unknown duration sorts after known durations for
duration order.

`meta.pending_dates` still protects completeness. When non-empty, the endpoint
returns no items or cursor. The frontend renders a precise unavailable state;
it never shows partial rows, retries the raw query, or claims the resource data
is materializing normally.

`UnmatchedResourceTable` is renamed internally to the neutral resource
breakdown table. It receives and displays the six columns above, uses the
existing currency/duration/label formatters, and adds `Load more` below the
table. Changing Owner, date/source filters, service, or sort resets the cursor
and prior pages.

## Rollout

1. Apply the Cost Insight additive migration before deploying either reader or
   writer code.
2. Deploy source importers and automatic rematerialization code.
3. Reimport the last 31 stable usage days for each active GCP/AWS source,
   refresh attribution for the same days, then run
   `materialize-resource-serving` once over that range. Verify every expected
   source/date has a native publication before deploying the Dashboard UI.
4. Deploy the Dashboard API and UI. Existing old clients remain supported by
   the endpoint's default first page.
5. Observe a normal daily refresh and verify it republished every touched date
   without a manual materializer invocation.

Rollback redeploys the prior Dashboard reader only. It must not restore the
legacy raw join. If a new publication cannot be built, the panel remains
explicitly unavailable until the bounded materializer repair succeeds.

## Acceptance tests

### Cost Insight

- AWS resource projections retain `i-…` as `resource_id`, preserve S3 bucket
  name/tag fallback, and retain display labels; GCP projections retain global
  resource IDs.
- identifier enrichment updates raw rows without duplicate cost; the bounded
  reimport removes historical identifier-less rows.
- a serving version copies IDs, aggregates one concrete resource over multiple
  days/services, sums cost and available duration (`NULL` only when every
  contribution has no duration), and keeps fallback IDs null and grouped by
  source-fact identity rather than resource name.
- the dashboard list-cost expression excludes GCP flexible-CUD list cost while
  preserving negative adjustments and source/serving conservation.
- GCP resource sync, AWS resource sync, and attribution refresh each invoke
  source-scoped rematerialization for only their vendor, account, and requested
  dates, including empty dates; a mid-run failure marks the source job failed,
  retains already published windows, and leaves the remaining windows pending.

### CI Dashboard

- two daily serving facts with the same resource group return one item whose
  labels come from the greatest-absolute-cost row under the documented
  tie-breakers, whose service list is lexical, and whose ID, name, list cost,
  and duration (including all-NULL duration) match the aggregation contract;
- under a `service_name` filter, the Service column shows only that service;
  without it, it shows the full contributing service set;
- a fixture with an EC2 `i-…` ID and S3 name displays both fields; an absent ID
  is explicit;
- all paginated pages are deterministic and exhaustive, with no duplicate or
  skipped resource at a sort tie;
- the Owner drilldown total (across pages) equals the Cost-share fixture under
  the same filters, including the flexible-CUD exclusion;
- a pending source/date returns no partial data and never executes raw-ledger
  reads;
- frontend tests cover rendered columns, reset/load-more behavior, and the
  actionable pending state.

Run both enforced coverage gates and keep each at or above 90%:

```bash
cd cost-insight && python3 -m pytest
cd cost-insight && python3 -m ruff check src tests
cd ci-dashboard && make test-cov
cd ci-dashboard && make lint
cd ci-dashboard/web && npm test && npm run build
```
