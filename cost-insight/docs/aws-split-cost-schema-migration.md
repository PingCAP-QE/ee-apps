# AWS 946646677266 Split-Cost Source Adaptation Design

> Status: proposed for implementation.
> Scope: only AWS account `946646677266`.
> Related: `docs/system-design.md`, `docs/bigquery-cost-optimization-design.md`, and
> `docs/label-allocation-design.md`.

## 1. Decision

Move AWS account `946646677266` to the split-cost source in Cost Insight:

```text
pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost
```

Implement this as an account-specific adapter in Cost Insight. Do not replace the
legacy AWS table globally and do not create a BigQuery migration job that attempts
to make the new table look like the old table.

The adapter must do more than rename columns. The new source contains both the
original EC2 parent cost and separate EKS pod split rows. It must emit one
normalized cost stream that preserves the source total while assigning the split
part to pods:

```text
normalized direct cost
  = ordinary direct rows
  + (parent direct cost - sum(all matching child split cost))
  + all emitted split-child cost
```

This is the only acceptable accounting invariant. Importing both parent direct
cost and emitted split-child cost double counts. Importing only split-child cost
loses the parent residual. The current source emits EKS pod children, but a
future non-pod `split_child` row follows this same invariant and carries its
own cost.

The upstream `resource_tags_user_icost_*` fields map at the adapter boundary to
the canonical Cost Insight dimensions `owner`, `service`, `project`, and
`service_exec_id`. They are not a prerequisite for EKS cost allocation. A pod
without resource requests can legitimately have zero split usage and zero split
cost; Cost Insight must not assign it a share of its parent node cost.

## 2. Context And Verified Facts

### 2.1 Sources

| Item | Legacy source | New source |
| --- | --- | --- |
| BigQuery table | `gcp-digital-bi.stg_cloud_billing.stg_aws_billing` | `pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost` |
| Scope | shared legacy AWS staging table | one AWS account: `946646677266` |
| Location | `us-west2` | `US` |
| Tag representation | `tag_*` plus nested `resource_tags.key_value` | flat `resource_tags_user_*` and `resource_tags_aws_*` columns |
| EKS pod allocation | unavailable | `split_line_item_*` columns and EKS workload tags |

The old and new sources are not drop-in schema replacements. In particular, the
new source has no `billing_month`, nested `resource_tags`, or
`line_item_net_unblended_cost`.

### 2.2 Snapshot Validation

The following reconciliation was checked on 2026-08-17 for the complete,
stable 14-day range `2026-08-02` through `2026-08-15` inclusive. The
`2026-08-16` partition was still incomplete and is deliberately excluded. The
new table was observed to contain earlier data (at least `2026-07-27`), but
that observation is not a production cutover decision or a history-completeness
claim.

| Check | Result |
| --- | ---: |
| legacy source list cost | $12,114.189006536670 |
| split source direct-or-parent list cost, all line-item types | $12,114.189005270325 |
| source list-cost difference | -$0.000001266345 |
| parent resource/day pairs with pod children | 7,894 |
| parent direct list cost, all line-item types | $8,486.987709478202 |
| AWS pod source split list cost | $4,708.764705669184 |
| parent residual list cost | $3,778.223003809018 |
| parent residual share of linked parent list cost | 44.51783287% |
| parent resource/days where child cost exceeded all parent direct cost | 0 |

The list-cost match is expected to floating-point noise only. It requires all
direct/parent line-item types: `SavingsPlanCoveredUsage` alone contributes
$7,050.61509963 of list cost in this range. Filtering the parent/direct side to
`Usage` drops valid list cost and falsely reports child-over-parent violations.

Effective cost is a separate source-semantic check. The split source represents
`SavingsPlanNegation`, but did not emit the legacy source's
`PrivateRateDiscount` and `EdpDiscount` rows in this range. Therefore source
internal effective-cost conservation is required, while legacy-to-split
effective/net/credit parity is not an acceptance criterion without an explicit
upstream decision.

For the validated node `i-0ef88ef97606efb63`, small positive pod allocations
exist on the same node, while `tiworkload-agent` has exact zero
`reserved_usage`, `actual_usage`, `split_usage`, and split cost. This is
consistent with a workload that has no Kubernetes resource requests. It is not a
rounding threshold or a depleted shared cost pool.

### 2.3 Field Mapping

| Normalized meaning | Legacy source | Split-cost source |
| --- | --- | --- |
| export partition month | `PARSE_DATE('%Y%m%d', billing_month)` | `DATE(bill_billing_period_start_date)` |
| owner fallback | `tag_used_by` | `resource_tags_user_usedby` |
| canonical owner | source-schema capability; not surfaced by current legacy adapter | `resource_tags_user_icost_owner_email` |
| org | `tag_tenant` | `resource_tags_user_tenant` |
| project fallback | `tag_project` | `resource_tags_user_project` |
| canonical project | `tag_icost_project` | `resource_tags_user_icost_project` |
| canonical service | source-schema capability; not surfaced by current legacy adapter | `resource_tags_user_icost_service` |
| canonical service execution | source-schema capability; not surfaced by current legacy adapter | `resource_tags_user_icost_service_exec_id` |
| logical cluster | `tag_cluster` | `resource_tags_user_cluster` |
| shared pool | nested `user_shared_pool` | `resource_tags_user_shared_pool` |
| EKS namespace | unavailable | `resource_tags_aws_eks_namespace` |
| EKS workload name/type | unavailable | `resource_tags_aws_eks_workload_name`, `resource_tags_aws_eks_workload_type` |
| parent resource | unavailable | `split_line_item_parent_resource_id` |
| direct effective cost | `line_item_unblended_cost` | `line_item_unblended_cost` |
| pod effective cost | unavailable | `split_line_item_split_cost` |
| direct list cost | `pricing_public_on_demand_cost` | `pricing_public_on_demand_cost` |
| pod list cost | unavailable | `split_line_item_public_on_demand_split_cost` |

The split-cost source has no net-unblended-cost field. For this adapter,
`net_cost = effective_cost` and `credit_amount = 0`. This states the source
limitation explicitly instead of manufacturing credit values. It changes the
persisted metric semantics after the approved production cutover: invoice
credits that were represented by the legacy source's net-unblended-cost field
will no longer reduce `net_cost`, and `credit_amount` will become zero for
split-source rows. Because `net_cost` is also the shared-pool allocation weight
in `refresh_attribution_daily.py`, this is a reporting and comparability
impact, not just a source-field substitution. The cutover must include a
net-cost / credit reconciliation across the boundary and ongoing monitoring for
an unexpected change beyond this documented effect.

## 3. Goals And Non-Goals

### Goals

1. Use the split-cost table for all new `946646677266` imports from the
   approved production cutover date onward.
2. Preserve account/day/list/effective cost totals exactly, within an `1e-8`
   currency tolerance before TiDB storage.
3. Attribute EKS pod split cost to its workload and direct canonical business
   dimensions.
4. Preserve the unallocated portion of an EKS node as a parent residual rather
   than dropping or silently redistributing it.
5. Keep legacy AWS sources and GCP sources working without a schema switch.
6. Preserve fractional pod costs through the Cost Insight summary and
   attribution tables.

### Non-Goals

1. Retroactively allocate zero-usage pods, including pods without resource
   requests.
2. Infer missing direct business dimensions from Kubernetes metadata.
3. Reconstruct invoice credits not present in the split-cost table.
4. Store raw CUR columns in TiDB or replace BigQuery as the investigation
   source of truth.
5. Change TCMS shared-pool allocation rules. Direct source labels win before
   that fallback is considered.
6. Make a derived parent-residual allocation the default source fact. That is
   an opt-in reporting policy defined in Section 6.2.

## 4. Architecture

### 4.1 Per-Account Source Profile

The current `AwsBillingSettings.billing_table` is global, while this source is
only valid for one AWS account. Changing `DEFAULT_AWS_BILLING_TABLE` would
silently point every future AWS source at an account-specific table. Do not do
that.

Extend `cost_sources` with the physical source profile:

```sql
ALTER TABLE cost_sources
  ADD COLUMN IF NOT EXISTS source_table VARCHAR(512) NULL AFTER display_name,
  ADD COLUMN IF NOT EXISTS source_schema_version VARCHAR(64) NULL AFTER source_table,
  ADD COLUMN IF NOT EXISTS source_available_from DATE NULL AFTER source_schema_version;

UPDATE cost_sources
SET
  source_table = 'pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost',
  source_schema_version = 'aws_split_cost_v1',
  source_available_from = DATE '<approved-production-cutover-date>'
WHERE vendor = 'aws' AND account_id = '946646677266';
```

`<approved-production-cutover-date>` is intentionally a placeholder. Do not
apply this profile change in the shadow phase; set it only after the recorded
shadow acceptance. `NULL` remains backward compatible: AWS sources without a
profile use the current `COST_INSIGHT_AWS_BILLING_TABLE` and
`aws_cur_legacy_v1`. The env var is therefore a legacy fallback, not the
configuration for this new account. The `aws/946646677266` source row is created by
`sql/002_seed_initial_cost_sources.sql`; the profile update above is a
production-promotion migration and must affect that existing row. Do not run it
in the shadow phase or after a rollback unless re-promoting the split source.

Extend `CostSource` and the AWS CLI source resolver to return an
`AwsBillingSource` value containing `account_id`, `billing_table`,
`schema_version`, and `available_from`. The query dispatcher selects the
adapter by `schema_version`; no account ID conditional belongs in SQL builders.

### 4.2 Adapter Boundary

Keep the existing legacy adapter in `sources/aws_billing_export.py`. Add a
separate split-cost adapter, for example `sources/aws_split_cost_export.py`,
with the same fetch contract as the legacy adapter:

```python
Iterator[dict[str, Any]]
```

The sync jobs continue to own watermarking, batching, upserts, and job state.
They receive the resolved source profile and delegate only BigQuery query
construction and row retrieval to the selected adapter.

This boundary keeps legacy SQL understandable and makes a future CUR schema
change a new adapter rather than a growing set of conditionals.

### 4.3 Target Data Model

Add migration `sql/010_add_aws_split_cost_dimensions.sql`.

```sql
ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS source_schema_version VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS namespace VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL,
  ADD COLUMN IF NOT EXISTS owner VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS parent_resource_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL,
  ADD COLUMN IF NOT EXISTS owner VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS namespace VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL;

CREATE INDEX IF NOT EXISTS idx_cost_attribution_source_scope_date
  ON cost_attribution_daily (vendor, account_id, source_allocation_scope, usage_date);
```

`cost_bq_export_summary_daily` stores canonical direct business dimensions,
not upstream tag-key names. `source_schema_version` is populated as
`aws_split_cost_v1` only by the split-cost adapter. Attribution applies direct
`owner`/`service`/`project`/`service_exec_id` source-label precedence only for
that marker, so future GCP or legacy source changes cannot silently bypass
TCMS. `cost_attribution_daily` already has the same
resolved `owner`, `service`, `project`, and `service_exec_id` dimensions, so it
does not duplicate the source columns. `attribution_source` and
`allocate_method` preserve whether their values came directly from the source,
from TCMS, or from a fallback.

`source_allocation_scope` is one of:

| Value | Meaning |
| --- | --- |
| `direct` | normal source line with no split child relationship |
| `eks_pod` | cost emitted by the EKS pod split record |
| `eks_parent_residual` | direct parent cost remaining after pod split cost |
| `split_child` | future non-pod split child; retained instead of silently discarding it |

`allocate_method` in `cost_attribution_daily` remains the Cost Insight/TCMS
attribution mechanism. It must not be overloaded with source allocation scope.

### 4.4 Currency Precision

Pod costs can be much less than one cent. The existing `DECIMAL(16,2)` storage
would turn valid split allocations into zero after grouping by workload.

In the same migration, change all four monetary columns in the summary,
unmatched-resource, and attribution tables to `DECIMAL(24,8)`. This preserves
at least the original 14 integer digits while adding eight fractional digits:

```sql
ALTER TABLE cost_bq_export_summary_daily
  MODIFY COLUMN list_cost DECIMAL(24,8) NULL,
  MODIFY COLUMN effective_cost DECIMAL(24,8) NULL,
  MODIFY COLUMN credit_amount DECIMAL(24,8) NULL,
  MODIFY COLUMN net_cost DECIMAL(24,8) NULL;
-- Apply the same four MODIFY COLUMN operations to cost_unmatched_resource_daily
-- and cost_attribution_daily.
```

Widening `DECIMAL(16,2)` to `DECIMAL(24,8)` can require a copy-style table
rebuild and block writers. Run this migration in a maintenance window, or use
an approved online-DDL tool such as `gh-ost` or `pt-online-schema-change` for
the continuously written tables, especially `cost_attribution_daily`.

BigQuery must aggregate with full `NUMERIC` precision and return at most eight
decimal places. Do not use `ROUND(SUM(...), 2)` in the split-cost queries.
Dashboard display can round to cents; persisted facts must not. This also
applies to the existing TCMS shared-pool allocation path in
`src/cost_insight/jobs/refresh_attribution_daily.py`: replace its
`ROUND(..., 2)` calls for weighted and equal shared-pool allocations with
full-precision expressions before writing the attribution rows. Rows routed
through shared-pool allocation are included in the eight-decimal precision
guarantee.

## 5. Split-Cost Query Semantics

### 5.1 Normalized Dimensions

For every source row, normalize empty strings with `NULLIF(TRIM(value), '')`.

```text
owner = resource_tags_user_icost_owner_email
service = resource_tags_user_icost_service
project = COALESCE(resource_tags_user_icost_project, resource_tags_user_project)
service_exec_id = resource_tags_user_icost_service_exec_id
author = COALESCE(owner, user_usedby)
org = user_tenant
repo = project  # legacy compatibility projection, not the canonical project field
vendor_tags_json = {cluster, shared_pool}
```

`service_name` remains the cloud billing service, such as Amazon EC2. Never
replace it with `service`; the latter is a business attribution dimension and
is written to `cost_attribution_daily.service`.

The `icost_` prefix is an upstream tag-key convention only. The adapter maps
those source columns to the canonical names above before they reach TiDB; no
`icost_*` column is added to Cost Insight tables. `repo = project` is temporary
compatibility for existing reports that historically interpreted the AWS project
tag as a repository. New attribution and reporting logic must use `project`.

Every output branch must provide the common summary-row contract. Set
`billing_account_id` to `NULLIF(bill_payer_account_id, '')` (and keep
`account_id` as the usage account). Ordinary direct rows retain the legacy AWS
SKU and source-time mappings: `sku_name` is
`COALESCE(product_sku, line_item_usage_type, line_item_line_item_description)`
and `source_export_time` is `line_item_usage_end_date`. Parent-residual rows
use the synthetic `EKS:ParentResidual` SKU marker and the maximum contributing
parent `line_item_usage_end_date`. Pod and non-pod child rows inherit the
matched parent's SKU and use the maximum contributing parent or child
`line_item_usage_end_date`; this ensures either side's correction advances the
upserted source timestamp.

The EKS namespace/workload fields and canonical business dimensions are
independent summary dimensions, not opaque JSON. `vendor_tags_json` stays
limited to allocation routing tags (`cluster` and `shared_pool`) so existing
TCMS JSON subset matching does not accidentally become dependent on pod
metadata.

### 5.2 Three Output Branches

The split adapter operates first at this grain:

```text
(account_id, usage_date, resource_id)
```

This is intentionally a daily key. In the validated source, EKS child intervals
are not a one-to-one match with EC2 line-item intervals: only 5,488 of 12,583
child-parent intervals matched exactly, while all 12,583 matched by parent
resource and UTC usage date. The source nevertheless conserves parent direct
cost against child split cost at the resource/day grain, which is also the
target grain of Cost Insight. A timestamp-level join would therefore lose valid
split rows.

1. **Ordinary direct rows**

   Emit direct rows whose `line_item_resource_id` is not a parent referenced by
   a split child on the same usage date. Aggregate every direct source row,
   regardless of `line_item_line_item_type`; this includes
   `SavingsPlanCoveredUsage`, `SavingsPlanNegation`, and future direct
   adjustments emitted by the source. Split-child list cost is a reallocation
   of its parent direct list cost and must never be added to this direct total.
   Use:

   ```text
   list_cost = pricing_public_on_demand_cost
   effective_cost = line_item_unblended_cost
   net_cost = line_item_unblended_cost
   credit_amount = 0
   source_allocation_scope = direct
   ```

2. **EKS parent residual**

   Aggregate all direct parent cost for every resource/day referenced by a
   split child, then subtract all matching child split costs. The parent side
   includes every direct line-item type. Emit exactly one residual record per
   parent/day with the parent tags and
   `source_allocation_scope = eks_parent_residual`.

   ```text
   residual_list = parent_direct_list - sum(child_public_on_demand_split_cost)
   residual_effective = parent_direct_unblended - sum(child_split_cost)
   residual_net = residual_effective
   residual_credit = 0
   ```

   Run the child-over-parent guardrail before emitting this row. If a residual
   is negative but no less than `-1e-8`, clamp that money field to zero to avoid
   persisting floating-point reconciliation drift as negative cost. A residual
   below `-1e-8` is a guardrail failure and must not be clamped or written.

   Use the parent service and region. Give this synthetic row a stable
   `usage_type`/SKU marker such as `EKS:ParentResidual`; the existing driver
   classifier will still classify the Amazon EC2 service as compute.

3. **Split children**

   Emit one aggregate per pod workload/label dimension using:

   ```text
   list_cost = split_line_item_public_on_demand_split_cost
   effective_cost = split_line_item_split_cost
   net_cost = split_line_item_split_cost
   credit_amount = 0
   source_allocation_scope = eks_pod
   ```

   The billing service and region come from the matched parent, so an EC2 node
   allocation remains visible as compute cost rather than as an artificial
   Amazon EKS product charge. `resource_name` in the unmatched-resource table
   is the pod resource ID; `parent_resource_name` is the EC2 instance ID.
   For pod split rows, `usage_seconds` is `split_usage` converted using the
   matched parent's `pricing_unit`: multiply hours by 3600, minutes by 60, and
   seconds by 1; otherwise store `NULL`, matching the legacy mixed/unknown-unit
   behavior. `reserved_usage` and `actual_usage` remain source diagnostics and
   are not substituted for `split_usage` in the unmatched-resource fact.

   A non-pod child is retained with `source_allocation_scope = split_child` and
   its own labels. This makes a future AWS split consumer visible for review
   instead of losing cost due to an EKS-specific filter.

Rows with all four normalized money fields equal to zero may be omitted from
cost facts. This intentionally excludes zero-request pods such as the observed
`tiworkload-agent` records. Their parent residual remains, and no cost is
invented for the pod.

### 5.3 Guardrails

The adapter must fail the import before writing if either of these exact
comparisons is true for any parent resource/day. In both comparisons, parent
direct cost is the all-line-item-type aggregate defined in the parent-residual
branch:

```text
sum(child list split cost) - parent direct list cost > 1e-8
sum(child effective split cost) - parent direct effective cost > 1e-8
```

Do not use `GREATEST(parent - child, 0)` to hide a violation. Emit the parent
resource IDs, usage dates, and amounts in the failure log for investigation.

The final summary query groups only after the three branches are normalized.
For split-source rows, its grouping and `source_row_hash` include all of:

```text
source_allocation_scope
namespace, workload_name, workload_type
owner, service, project, service_exec_id
vendor_tags_json
```

Without these hash dimensions, independently labelled workloads can collide in
the existing summary upsert. Do not extend the shared `HASH_FIELDS` used by the
GCP and legacy-AWS paths globally: that would change their hashes without
removing old-hash rows and could double count those sources on the next sync.
Use a split-source-specific hash field list or builder, and keep the existing
GCP/legacy-AWS hash inputs unchanged. If the shared hash code must be changed,
perform a full partition replacement for those sources rather than an
incremental upsert.

Apply the same split-only identity rule to unmatched-resource rows. Their
split-source `source_row_hash` must include `source_allocation_scope`,
`parent_resource_name`, `namespace`, workload fields, canonical business
dimensions, and
`vendor_tags_json`, while the existing GCP unmatched `HASH_FIELDS` remain
unchanged. Thus rows that differ in split allocation scope or parent cannot
collide during an unmatched-resource upsert.

## 6. Attribution Rules

### 6.1 Source Attribution

Update the split-aware TCMS summary attribution builders,
`_build_insert_attribution_daily_from_summary_with_tcms` and
`_build_insert_shared_attribution_daily_from_summary`, to preserve
`source_allocation_scope`, namespace, workload name, and workload type in the
final grouping and `dimension_hash` for split-source rows. Do not add those
fields to `_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY`: it is the non-TCMS GCP
path, where the new fields are always `direct`/`NULL`, and changing it would
unnecessarily churn persisted GCP `dimension_hash` values. `_INSERT_ATTRIBUTION_DAILY`
is the raw-based insert used by `run_refresh_cost_attribution_daily`, not the
TCMS summary path, and the split source does not populate that raw path.

For a summary row, resolve labels in this order:

1. Direct canonical business dimensions from the split-cost source.
2. Most-specific matching TCMS allocation from `vendor_tags_json`.
3. Legacy source `author`/roster matching.
4. Existing unattributed fallback.

Direct source-dimension behavior:

```text
owner / match_identity = summary.owner
identity_kind = source_label
service = summary.service
project = summary.project
service_exec_id = summary.service_exec_id
attribution_source = source_label
allocate_method = direct_label
```

If only project or service exists, preserve those fields while owner matching
falls through to the next available owner source. A TCMS mapping must not
overwrite any non-null direct canonical business dimension. This allows the AWS
source to carry explicit business attribution while retaining TCMS as a fallback
for legacy resources.

Existing author matching continues to support a full email address, so using
the canonical `owner` as the normalized `author` fallback is compatible with the
roster join. The direct-source precedence is retained through
`attribution_source = source_label`, not by persisting an upstream tag-key name.

### 6.2 Optional Parent-Residual List-Cost Policy

The source fact remains authoritative and is not rewritten: AWS pod split list
cost and the parent residual are both retained exactly as emitted/derived by the
three-branch adapter. The residual can additionally be allocated to pods for a
specific *list-cost reporting policy*. It is not an AWS source allocation and
must never be presented as one.

The allocation must be calculated from an adapter-side allocation ledger at
`(usage_date, account_id, parent_resource_id, pod_resource_id)` grain, before
the summary's broader grouping loses the parent ID. Do not attempt to reconstruct
this relation from `cost_bq_export_summary_daily`: it has intentionally
aggregated across resource IDs. Materialize an auditable derived fact, for
example `cost_aws_parent_residual_allocation_daily`, with at least:

```text
usage_date, vendor, account_id
parent_resource_id, pod_resource_id
namespace, workload_name, workload_type
owner, service, project, service_exec_id
source_pod_split_list_cost
parent_direct_list_cost, parent_residual_list_cost
allocation_weight, derived_parent_residual_list_cost
allocation_origin = cost_insight_derived
allocation_method = proportional_source_split_list_v1
allocation_version, parent_input_hash, calculated_at
```

`allocation_weight` is stored as `DECIMAL(32,24)` and quantized to that scale
before calculating a derived amount. The final pod keeps the deterministic
eight-decimal rounding remainder so every parent/day conserves its residual;
the stored derived amount, rather than a recomputation from the weight alone,
is therefore the authoritative audit value for that final row.

`parent_input_hash` identifies the parent/day direct total and the complete set
of source pod split inputs. It makes a correction in BigQuery detectable and
makes the derived result reproducible. The fact stores the source pod split and
the Cost Insight-derived residual in separate fields; it does not replace an
`eks_pod` or `eks_parent_residual` source row.

This is a one-row-per-pod allocation audit. `parent_direct_list_cost` and
`parent_residual_list_cost` are intentionally repeated for each participating
pod. Any parent-level report over this table must use `MAX` per
`(usage_date, account_id, parent_resource_id, parent_input_hash)`, not `SUM`.
The derived pod column is the only cost column that is summed across its rows.

For each parent/day, let `S_i` be a pod's AWS source split list cost, `D` be the
sum of all positive `S_i`, and `R` be the all-line-item-type parent residual
list cost:

```text
if D > 0:
  allocation_weight_i = S_i / D
  derived_parent_residual_list_cost_i = R * allocation_weight_i
else:
  retain R as parent residual; emit no derived pod allocation
```

Only pods with positive source split list cost participate. Consequently a
zero-request pod such as `tiworkload-agent` has zero weight and receives no
derived cost. A negative residual, if one occurs, uses the same positive-pod
weights so the parent/day still conserves; a zero denominator is never replaced
with equal weights.

Calculate at high precision, round to the persisted eight-decimal currency
scale only at the end, and assign the final rounding remainder to the
lexicographically last `pod_resource_id` for that parent/day. The required
post-rounding invariant is:

```text
sum(derived_parent_residual_list_cost) = parent_residual_list_cost
```

This first policy is intentionally list-cost only. Applying the same weights to
effective, net, or credit cost is a separate product decision because the split
source does not have legacy-equivalent net/discount semantics.

Expose two explicitly named reporting paths:

| Path | Includes | Prohibited sum |
| --- | --- | --- |
| raw source view | AWS `eks_pod` list cost and explicit `eks_parent_residual` list cost | do not add derived allocation to it |
| fully allocated list-policy view | ordinary direct list cost, AWS pod split list cost, derived parent residual list cost, and any unallocatable residual | do not add raw `eks_parent_residual` list cost for a parent/day that has derived allocations |

The fully allocated view must expose `aws_source_pod_split_list_cost`,
`derived_parent_residual_list_cost`, and `fully_allocated_list_cost` as separate
columns, plus `unallocated_parent_residual_list_cost` for a parent/day with no
eligible pod. It must use the same direct-label/TCMS/author resolution order as
Section 6.1, and label derived rows with `allocation_origin =
cost_insight_derived`. Phase one only materializes and audits the derived fact;
it does not change existing dashboards or the canonical
`cost_attribution_daily` total. Enabling the policy view requires an explicit
reporting approval after its own reconciliation.

## 7. Shadow Validation, Cutover, And Backfill

### 7.1 Fixed Two-Week Shadow Window

Use `2026-08-02` through `2026-08-15` inclusive for the first validation. It
is the latest 14-day window known to be complete at validation time. Do not use
`2026-08-16` until the upstream partition is stable.

Before running the split adapter, copy the production legacy rows for this
account/window into a fixed, read-only snapshot table:

```text
cost_summary_aws_7266_legacy_20260802_20260815
```

Run the split adapter into a separate fixed shadow table:

```text
cost_summary_aws_7266_split_20260802_20260815
```

Create equivalent snapshot/shadow tables named
`cost_unmatched_aws_7266_legacy_20260802_20260815` and
`cost_unmatched_aws_7266_split_20260802_20260815` for
`cost_unmatched_resource_daily` when resource-level validation is enabled. The
names are abbreviated because TiDB/MySQL table identifiers are limited to 64
characters. The tables use the production schema plus the migration in Section
4.3, retain the fixed date range in their names, and are not subject to the
normal job's cleanup or watermark handling.

The shadow invocation must not update the production `cost_sources` profile,
job state, `cost_bq_export_summary_daily`, `cost_unmatched_resource_daily`, or
`cost_attribution_daily`. Implement a dedicated, allowlisted validation target
(or an equivalently validated `--target-summary-table` option); never interpolate
an arbitrary CLI table identifier into SQL. The selected target determines all
summary, unmatched-resource, and optional attribution shadow tables.

The implementation exposes only this fixed target through these commands:

```bash
python -m cost_insight.jobs.cli snapshot-aws-split-cost-shadow-legacy
python -m cost_insight.jobs.cli sync-aws-split-cost-shadow
```

The shadow commands do not update production facts, `cost_sources`, job state,
or attribution. A production promotion uses the separate profile-gated command:

```bash
python -m cost_insight.jobs.cli cutover-aws-split-cost \
  --usage-start-date <approved-cutover-date> \
  --usage-end-date <approved-window-end>
```

### 7.2 Shadow Acceptance

Keep both fixed tables. Do not delete or replace production rows during this
phase. Record and approve all of the following before promotion:

1. Legacy raw list cost versus new raw direct-or-parent list cost, using all
   direct line-item types, is within `$0.00001` for every day and the full
   window. The observed full-window difference is `$0.000001266345`.
2. New raw direct/parent list cost equals the split shadow normalized list cost.
   Pod child list cost is not added to the raw parent direct total in this
   comparison.
3. For every parent/day, `parent residual + AWS pod split = parent direct`, no
   child-over-parent guardrail fails, and daily direct/pod/residual subtotals
   are recorded.
4. At least five stable direct resources across EC2, S3, and EBS match the
   legacy source by day for list cost. An EKS node is checked as parent residual
   plus all source pod split rows.
5. The shadow summary preserves the canonical owner/service/project/
   service-execution, namespace, workload, scope, and eight-decimal split-cost
   dimensions. Net/effective/credit differences are
   reported separately as the documented source-semantic change, not silently
   treated as a list-cost mismatch.
6. When the optional residual policy is evaluated, its audit fact conserves
   every eligible parent/day and reports the number and value of zero-denominator
   parents left as raw residual.

### 7.3 Production Promotion And Rollback

After shadow approval, choose and record an approved production cutover date.
Update the account source profile with that date, then use a split-source
replacement mode that atomically:

1. reads and spools normalized split-source rows for the selected usage-date
   window and `usage_date >= source_available_from`;
2. deletes production summary and unmatched-resource rows only for the same
   `(vendor, account_id, usage_date)` window;
3. writes the spooled split rows; and
4. refreshes production attribution for exactly those dates.

The existing month-partition replacement stays unchanged for legacy sources.
Do not run `--replace-existing-partitions` against an entire billing month for
this migration. The legacy snapshot and the split shadow table stay intact
through the agreed rollback window; promotion must not clean them up.

Rows before the approved cutover remain imported from the legacy source. If the
new source is later proven complete for older history, validate and promote one
usage-date window at a time through the same process.

Rollback is data-preserving: set `source_schema_version` back to
`aws_cur_legacy_v1` and clear `source_table` and `source_available_from` so
legacy resolution cannot run legacy SQL against the split-cost table. Replace
only the promoted usage-date window with the preserved legacy source rows, then
refresh attribution. Do not mix both source schemas for the same account/date.

## 8. Verification And Acceptance Criteria

### 8.1 BigQuery Adapter Checks

For each stable usage date and for the full cutover window, verify:

1. `SUM(normalized effective_cost) = SUM(raw line_item_unblended_cost)` within
   `1e-8` under the same-source BigQuery `NUMERIC` calculation, with the raw
   aggregate including every direct/parent line-item type and excluding
   split-child rows (`split_line_item_parent_resource_id IS NULL`). This
   aggregate contains ordinary direct and parent direct rows only; child costs
   are represented by `split_line_item_split_cost` in the normalized output.
2. The equivalent list-cost equality holds against
   `SUM(pricing_public_on_demand_cost)` with the same all-line-item-type direct
   and split-child exclusion. Do not add split child list cost on top of the
   parent/direct raw total.
3. No parent resource/day violates the child-over-parent guardrail.
4. `SUM(parent residual) + SUM(pod split) = SUM(parent direct)` for each
   parent resource/day.
5. At least five stable direct resources across EC2, S3, and EBS match the
   legacy source's list cost for the same day. For EKS nodes, compare parent
   residual plus pod split, not the parent row alone.
6. Small nonzero pod split costs survive at eight decimal places.
7. Across the approved cutover, reconcile `net_cost` and `credit_amount`
   separately from list cost, confirm the documented source-semantic change,
   and alert on deviations that cannot be explained by the split source's
   missing net/discount fields; also verify shared-pool allocation weights
   reflect the documented `net_cost` change.

### 8.2 Attribution Checks

1. Pod split rows retain namespace, workload name, workload type, and
   `source_allocation_scope = eks_pod` through `cost_attribution_daily`.
2. A row with all direct source business dimensions populates final
   owner/service/project from those values and reports `source_label`.
3. A row without direct labels still uses TCMS, then legacy author fallback.
4. A zero-request pod has zero cost and does not cause a corresponding parent
   residual to be assigned to that pod.
5. Sum of `cost_attribution_daily` cost fields equals its input summary range
   after refresh, subject only to the existing shared-pool allocation rules.
6. The optional fully allocated list-policy view exposes source pod split and
   derived residual amounts separately, replaces raw parent residual only for
   parent/days with derived allocations, retains zero-denominator residual as
   explicitly unallocated, and conserves the residual at every parent/day after
   rounding.

### 8.3 Automated Tests

Add coverage for:

| Area | Required cases |
| --- | --- |
| source registry | legacy default source and account-specific split profile resolve independently |
| query builder | split schema uses flat tags and `bill_billing_period_start_date`; legacy SQL is unchanged |
| normalization | ordinary direct, pod child, parent residual, and non-pod child branches |
| guardrails | over-allocated parent raises and writes nothing |
| precision | a cost below `$0.01` survives summary normalization and TiDB binding |
| hashing | different workload/label/allocation-scope dimensions produce different hashes |
| attribution | split-source direct business dimensions override TCMS; legacy/GCP values preserve TCMS/author behavior |
| shadow target | fixed snapshot/shadow targets are selected from an allowlist; snapshot retry publishes only a completed table and no production table, job state, or profile is changed |
| replacement | split cutover deletes only the approved usage dates, not earlier dates in the same billing month |
| residual policy | proportional weights, zero-denominator retention, deterministic rounding remainder, source/derived separation, and parent/day conservation |

## 9. Implementation Plan

1. Add the TiDB migration, including the split dimensions and the optional
   parent-residual allocation audit fact.
2. Extend `CostSource`, source resolution, settings fallback, and AWS job
   signatures to carry the resolved profile.
3. Add the split-cost BigQuery adapter and the three-branch reconciliation
   query, plus the adapter-side parent/pod allocation ledger required by the
   optional policy.
4. Add fixed, allowlisted shadow targets and usage-date replacement support for
   split-source imports.
5. Extend summary and attribution normalization, hashing, inserts, and grouping
   with the new dimensions and direct-label precedence; explicitly update the
   unmatched-resource normalization, hash, and upsert path so
   `source_allocation_scope`, `parent_resource_name`, workload fields, and
   canonical business dimensions are populated and included in the split-source
   hash there as well.
6. Add the fully allocated list-policy view only after the audit fact is
   reconciled and its reporting approval is recorded.
7. Add tests, run `make lint` and `make test` from `cost-insight/`.
8. Execute the snapshot, shadow reconciliation, approved limited promotion,
   and cutover sequence in Section 7.

## 10. Operational Ownership

The split-cost adapter reports what the upstream AWS allocation feed emitted.
For a pod with zero `reserved_usage`, `actual_usage`, and `split_usage`, the
owner is the workload/collector configuration, not Cost Insight. The first
diagnostic is the historical Pod/ReplicaSet specification, especially
`resources.requests.cpu`, `resources.requests.memory`, and init-container
requests, followed by collector visibility and pod identity matching.
