# Cost Data System Design

## Goals

Build a small, vendor-neutral cost data system that can:

- collect cost and usage details from cloud billing sources
- preserve enough line-item detail for arbitrary aggregation
- attribute CI usage and cost by `author`, `repo`, roster group, and manager
- compare attributed spend with period budgets later
- support GCP first, then AWS and other vendors without table renames

Non-goals for the first version:

- real-time cost enforcement
- complex budget workflow or Lark sync
- full historical roster snapshots
- a generic FinOps warehouse with every cloud-specific field promoted to columns

## Project Layout

The cost system should live in a new top-level project folder:

```text
cost-insight/
  docs/
  sql/
  src/cost_insight/
  tests/
```

This keeps cost independent from `ci-dashboard`. The two systems can share the
same TiDB instance and can join roster tables, but cost owns its own schema,
jobs, and API surface.

## Source Direction

Use BigQuery billing exports as the primary cost source.

For GCP, the current default (2026-10-27) is the warehouse Cloud Billing
Detailed usage export, queried in `us-west1`:

```text
warehouse-f80fbbe8e8.raw_gcp_billing.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6
```

References to `gcp-digital-bi.gcp_billing_detailed` in older validation examples
identify the legacy export, not the current default. See the
[warehouse cutover report](warehouse-billing-cutover-2026-10-27.md) for source
boundaries, identity verification and remaining rollout gates.

This source has resource-level fields, usage fields, labels, credits, list-cost
fields, price fields, and export timestamps. It is a better primary source than
Billing APIs because the system needs arbitrary aggregation and invoice-like
line items.

Current known facts for the first GCP source:

| Field | Value |
| --- | --- |
| GCP project to import | `pingcap-testing-account` |
| Earliest useful history | `2026-01-01` |
| Approximate source volume | about 330K billing export rows per day |
| Currency | `USD` |
| Last validated local access | available through `bq` from this workspace |

The ETL should aggregate this source before writing to TiDB. We do not need to
store every hourly export row locally.

Use APIs only as auxiliary sources:

- Billing Account API: account and project metadata
- Budget API or Lark: budget metadata later

Native GKE Cost Allocation is enabled for the current GCP project. Cloud
Billing Detailed Export is therefore both the invoice source and the
Kubernetes allocation source: it carries cluster, namespace, workload,
author/repo labels, direct workload list cost, and explicit residual classes.
The cost pipeline does not query GKE usage-metering tables.

Known useful labels in current GCP billing and usage exports:

| Source label | Normalized column | Usage |
| --- | --- | --- |
| `k8s-label/author` or `author` | `author` | CI person attribution |
| `k8s-label/org` or `org` | `org` | GitHub organization grouping |
| `k8s-label/repo` or `repo` | `repo` | repo-level budget and cost views |
| `k8s-namespace` or row namespace | `namespace` | CI vs system/platform grouping |
| Pod/resource name from source export | `resource_name` | Resource investigation when labels are missing |

Validation snapshot from the last 30 days:

| Check | Result |
| --- | --- |
| billing export rows for `pingcap-testing-account` | about 29.8M rows |
| rows with `author/repo/org` | about 18.2% |
| net cost with `author/repo/org` | about 26.6% |
| rows with namespace | about 92.0% |
| rows with resource or workload name | about 99.8% |
| net cost with resource or workload name | about 98.4% |

This means label enrichment is important, but the raw billing export already
has enough resource/workload naming to investigate most unallocated cost.

## Cost Terms

The system should expose four related amounts:

| Term | Meaning |
| --- | --- |
| `list_cost` | Cost at public/list price before negotiated discount |
| `effective_cost` | Cost after negotiated or contract discount, before credits |
| `credit_amount` | Credits, promotions, CUD/SUD credits, or adjustments; usually negative |
| `net_cost` | Actual charged/allocated cost after credits: `effective_cost + credit_amount` |

For GCP billing export:

| System field | GCP source field |
| --- | --- |
| `list_cost` | `cost_at_list` or `cost_at_list_consumption_model` |
| `effective_cost` | `cost` or `cost_at_effective_price_default` |
| `credit_amount` | `SUM(credits.amount)` |
| `net_cost` | `cost + SUM(credits.amount)` |

When a source does not provide all fields, leave the missing amount as `NULL`
instead of inventing values.

## Tables

### `cost_sources`

Stores the smallest cloud billing or usage sources that cost collectors are
allowed to import. For GCP this is a project; for AWS this is an account.

```sql
CREATE TABLE cost_sources (
  id BIGINT NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  billing_account_id VARCHAR(128) NULL,
  display_name VARCHAR(255) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_sources_vendor_account (vendor, account_id),
  KEY idx_cost_sources_billing_account (billing_account_id)
);
```

Sample:

| vendor | account_id | billing_account_id | display_name |
| --- | --- | --- | --- |
| `gcp` | `pingcap-testing-account` | `01ABCD-234EFG-567HIJ` | `PingCAP Testing` |
| `aws` | `123456789012` | `123456789012` | `QE CI AWS` |

Notes:

- `account_id` means GCP project ID for GCP and AWS account ID for AWS.
- `billing_account_id` is nullable because some usage-only sources might not
  know the billing account.
- No `environment` column for now. The source itself is already the minimum
  collection unit, and sub-classification should come from labels, repo, group,
  or manager.
- No generic `metadata_json` for now. If a concrete source attribute becomes
  necessary, add a named column or a separate mapping table then.

### `cost_bq_export_summary_daily`

Durable daily billing-import ledger for GCP and AWS. A row is scoped by export
partition, usage date, source account, and the normalized billing dimensions in
the source row hash. Late corrections can be imported without scanning or
rebuilding a deprecated TiDB raw-detail layer.

Regular collectors write this table, and attribution refreshes read it. The
source BigQuery billing exports remain authoritative for invoice-level or
one-off raw investigation. See
[BigQuery cost optimization design](bigquery-cost-optimization-design.md) for
the import and correction model.

### `cost_unmatched_resource_daily`

Weekly resource-level investigation data. It is separate from the regular
summary ledger because resource names, labels, and usage duration have much
higher cardinality and are not needed by normal dashboard summaries.

The Dashboard combines these rows with current attribution to show unmatched
resources. Historical invoice reconciliation remains in BigQuery.

### `cost_attribution_daily`

Daily attributed summary by author, repo, roster group, and manager.

```sql
CREATE TABLE cost_attribution_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  service_name VARCHAR(255) NULL,
  sku_name VARCHAR(255) NULL,

  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  resource_name VARCHAR(512) NULL,
  author VARCHAR(255) NULL,
  owner VARCHAR(255) NULL,
  attribution_key VARCHAR(255) NULL,
  attribution_source VARCHAR(64) NOT NULL,
  attribution_status VARCHAR(64) NOT NULL,

  employee_id BIGINT NULL,
  group_id BIGINT NULL,
  manager_id BIGINT NULL,

  usage_seconds DECIMAL(20, 2) NULL,
  list_cost DECIMAL(16, 2) NULL,
  effective_cost DECIMAL(16, 2) NULL,
  credit_amount DECIMAL(16, 2) NULL,
  net_cost DECIMAL(16, 2) NULL,
  source_rows BIGINT NOT NULL DEFAULT 0,
  dimension_hash CHAR(64) NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_attribution_daily_dimension_hash (usage_date, dimension_hash),
  KEY idx_cost_attribution_daily_author (usage_date, author),
  KEY idx_cost_attribution_daily_repo (usage_date, org, repo),
  KEY idx_cost_attribution_daily_group (usage_date, group_id),
  KEY idx_cost_attribution_daily_manager (usage_date, manager_id)
);
```

Sample:

| usage_date | account_id | service_name | sku_name | repo | author | resource_name | attribution_status | group_id | manager_id | usage_seconds | net_cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2026-05-18` | `pingcap-testing-account` | `Compute Engine` | `N1 Predefined Instance Core running in Americas` | `ticdc` | `liyishuai` | `cap-ticdc-pull-cdc-storage-integration-light-next-gen-318-qh26q` | `matched` | `42` | `7` | `65700.00` | `245.12` |
| `2026-05-18` | `pingcap-testing-account` | `Cloud Storage` | `Standard Storage US` | `NULL` | `NULL` | `NULL` | `unmatched` | `NULL` | `NULL` | `NULL` | `31.50` |

Attribution rules for V1:

1. CI usage uses a nonblank `author` as the primary identity; a nonblank
   `owner` is the fallback because current CI labels mainly use `author`.
2. Match the selected identity to `roster_employees.github_id` first.
3. If GitHub ID does not match, try `roster_employees.email` and email local-part.
4. A local-part or normalized fallback that matches multiple roster employees is
   ambiguous: do not choose arbitrarily or duplicate the cost; leave it unmatched.
5. Attach `group_id` and `manager_id` from the current active roster.
6. System namespaces such as `kube-system` and `flux-system` are marked
   `system` unless labels clearly identify an owner.
7. Unmatched rows stay visible with `attribution_status = 'unmatched'`.

### `cost_budgets`

Current production contract (verified 2026-11-24): budgets use **JSON account
membership**, not a scalar `account_id` column. Runtime readers and test fixtures
require this schema; scalar-account compatibility has been retired.

| Columns | Meaning |
| --- | --- |
| `id`, `budget_name` | Plan identity and display name |
| `vendor`, `accounts` JSON | Vendor and account membership; multiple accounts share one plan amount |
| `projects` JSON | Optional project membership; absent/empty falls back to `label_filters.project` in QA readers |
| `team`, `platform` | Plan classification; QA uses `QA`, CI uses `CICD` |
| `period_start_date`, `period_end_date`, `budget_amount` | Explicit period and total plan amount |
| `cost_basis` | Cost basis; see `029_add_ci_budget_cost_basis.sql` |
| `label_filters`, `filter_hash`, `scope_key` | Additional scope metadata and identity |
| `group_id`, `manager_id`, `repo` | Additional scope restrictions, where supported by the reader |
| `source_type`, `source_ref`, `created_at`, `updated_at` | Provenance and timestamps |

Source-wide plans must leave both `projects` and `label_filters` empty, with no
`group_id`, `manager_id`, or `repo` restriction. QA readers also recognize
`label_filters.project` as project membership when `projects` is empty; any other
label key or group/manager/repo restriction makes a plan currently non-reportable.
Account-only widgets additionally exclude all project-scoped plans.

A future sync should reuse `cost_insight.budgets.build_filter_hash` to compute
`filter_hash`: SHA256 over canonicalized `label_filters`, with keys and array
values sorted.

A QA example is `vendor='aws'`, `accounts=['a','b']`, `projects=['Alpha']`,
`platform='QA'`: its budget is shared across both accounts for Alpha, not a
separate full budget per account. QA reporting counts that plan once. The home
page's account-only budget widgets show only single-account plans; a shared plan
has no account allocation and is not duplicated into each member's budget.

Budget periods are explicit, usually annual. Weekly/monthly targets prorate the
period amount over calendar days. A missing applicable plan is `null`, not zero.
Project/group/repo restrictions must not be silently treated as unrestricted
account-wide plans by readers that cannot represent their scoped actual.

Historical SQL `001_create_cost_tables.sql` records the original scalar-account
DDL; do not rewrite it. The membership-schema transition is not currently present
in the numbered migration chain. For a fresh database, obtain/reconcile the
current budget DDL before applying migrations that depend on `accounts` and
`platform`; replaying the historical files alone is not a supported reconstruction
of the current budget table. No automated budget sync/seed CLI is implemented.

### `cost_job_state`

Tracks ETL watermarks and run status.

```sql
CREATE TABLE cost_job_state (
  job_name VARCHAR(128) NOT NULL,
  watermark_json JSON NOT NULL,
  last_started_at DATETIME NULL,
  last_succeeded_at DATETIME NULL,
  last_status VARCHAR(16) NOT NULL DEFAULT 'never',
  last_error TEXT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (job_name)
);
```

Sample watermarks:

| job_name | watermark_json |
| --- | --- |
| `sync_gcp_billing_summary:gcp:pingcap-testing-account` | `{"account_id":"pingcap-testing-account","export_partition_start":"2026-05-15","export_partition_end":"2026-05-18"}` |
| `refresh_cost_attribution_from_summary:aws:946646677266` | `{"vendor":"aws","account_id":"946646677266","start_date":"2026-05-01","end_date":"2026-05-31"}` |

## ETL Flow

### Flow 1: Billing Summary Import

```mermaid
flowchart LR
  Export["GCP/AWS billing export"] --> Summary["cost_bq_export_summary_daily"]
  Summary --> Attribution["summary attribution refresh"]
```

Steps:

1. Discover active sources from `cost_sources`.
2. Read bounded export partitions so BigQuery can prune source data.
3. Normalize provider fields and upsert by the stable source-row hash.
4. Refresh attribution for usage dates touched by imported partitions.
5. Advance the source watermark only after writes and attribution succeed.

### Flow 2: Resource Investigation Import

```mermaid
flowchart LR
  Export["GCP/AWS billing export"] --> Resource["cost_unmatched_resource_daily"]
  Resource --> Dashboard["unmatched resource investigation"]
```

Resource-level rows are imported separately on a bounded weekly window. This
keeps resource names, raw labels, and usage duration out of the normal billing
summary path.

### Flow 3: Attribution Refresh

Purpose: map cost and usage to author, repo, group, and manager.

```mermaid
flowchart LR
  Summary["cost_bq_export_summary_daily"] --> Rules
  Roster["roster_employees / roster_groups"] --> Rules
  Rules --> Daily["cost_attribution_daily"]
```

Rules:

1. Build a working set for affected dates.
2. Read normalized billing and allocation columns from the summary ledger.
3. Use nonblank `author` first, then nonblank `owner` for CI attribution.
4. Join the selected identity to `roster_employees.github_id`.
5. If GitHub ID does not match, try employee email and email local-part. A
   local-part or normalized fallback matching multiple employees remains
   unmatched rather than choosing one employee or duplicating cost.
6. Attach `group_id` and `manager_id` from current roster tables. If an
   employee manager is missing, fall back to the roster group's manager.
7. Aggregate by day, vendor, account, service, SKU, org, repo, resource, author,
   group, and manager.
8. Write unmatched data instead of hiding it.
9. Refresh is rebuildable by date range: delete existing attributed rows for
   the same `vendor/account/date` range, then insert the newly aggregated
   result. This keeps late billing corrections and roster fixes simple.
10. Run larger refreshes with `--split-by-day` so each TiDB query stays within
    the single-query memory quota.

Before enabling the ambiguity rules in a scheduled TiDB refresh, capture an
`EXPLAIN ANALYZE` for one representative day per statement variant: standard
(GCP and Tencent) and TCMS (AWS and Azure). The unique fallback lookup is a roster-only
derived table and must not regress either bounded refresh plan.

Current V1 attribution statuses:

| status | source | Meaning |
| --- | --- | --- |
| `matched` | `author_override` | `author` matched a configured bot override |
| `matched` | `author_github` | `author` matched roster GitHub ID |
| `matched` | `author_email` | `author` matched roster email or unique email local-part |
| `matched` | `author_normalized` | normalized `author` uniquely matched roster identity |
| `unmatched` | `author_label` | nonblank `author` exists but no roster employee matched |
| `matched` | `pvc_pod_override` | a mapped PVC Pod author matched a configured bot override |
| `matched` | `pvc_pod_github` / `pvc_pod_email` / `pvc_pod_normalized` | a mapped PVC Pod author matched roster identity |
| `unmatched` | `pvc_pod_author` | a mapped PVC Pod author did not match roster identity |
| `matched` | `owner_github` | no usable `author`; `owner` matched roster GitHub ID |
| `matched` | `owner_email` | no usable `author`; `owner` matched roster email or unique email local-part |
| `matched` | `owner_normalized` | no usable `author`; normalized `owner` uniquely matched roster identity |
| `unmatched` | `owner_label` | no usable `author`; nonblank `owner` exists but no roster employee matched |
| `unattributed` | `missing_author` | no nonblank `author`, mapped PVC author, or `owner` label exists |

Cost allocation with billing export:

1. Preserve provider-native GKE direct workload costs.
2. Allocate GKE idle and system-overhead residuals using positive native direct
   list-cost share inside the same day/project/cluster/SKU/component.
3. Preserve unsupported, unknown, control-plane, and no-participant residuals.
4. Refresh native owner/group attribution from the current roster.
5. Materialize three daily perspectives: Kubernetes allocated, EQ allocated,
   and Kubernetes then EQ allocated.
6. EQ chargeback uses same-day/vendor/account non-EQ native direct list-cost
   share. A zero denominator leaves the cost under EQ.
7. TCMS `shared_pool` remains visible metadata; it is not weighted or
   redistributed.

The Dashboard selects native or one published materialized perspective and
only aggregates it by week or month. Full rules and conservation contracts are
in [Unified cost allocation design](cost-allocation-unification-design.md).

### Flow 4: Budget Sync Later

Purpose: load period budgets from Lark or another source.

```mermaid
flowchart LR
  Lark["Lark budget table"] --> Sync["sync-budgets"]
  Sync --> Budgets["cost_budgets"]
  Budgets --> Compare["Budget vs actual API"]
  Attr["cost_attribution_daily"] --> Compare
```

Expected budget shapes:

- GCP `pingcap-testing-account`, `ticdc` annual CI budget
- GCP `pingcap-testing-account`, other CI annual budget
- group or manager period budget (currently non-reportable)
- repo-level period budget when a repo maps cleanly to a group (currently non-reportable)

A future sync must write the current `accounts`/`projects` membership contract,
plan classification, period, cost basis and identity metadata. It must not restore
scalar `account_id` budgets. Additional label/repo/group restrictions are supported
only by reports that can compute their scoped actual; do not infer allocations.

For monthly dashboards or alerts, calculate a derived monthly budget from the
period budget. For example, a yearly budget can be divided into 12 equal monthly
allocations unless the Lark source later provides a custom allocation curve.

Budget sync can wait until the cost and usage pipeline is stable.

## Current Implementation

1. Reconcile the current schema (including the externally evolved budget membership
   table described above), then apply the applicable migrations under `sql/`.
2. Import GCP and AWS billing partitions with the summary commands.
3. Refresh `cost_attribution_daily` from the summary ledger.
4. Import weekly resource investigation data separately.
5. Publish Kubernetes residual allocation facts after billing data settles.
6. Materialize and publish the three derived cost perspectives.
7. Add budget sync when a source of truth replaces the current manual rows.

## Source Identity Decision (2026-10-27)

Warehouse billing jobs use the existing `apps/cost-insight-gcp-billing` KSA,
directly bound by Workload Identity to
`hxprog-e5d756e67e8641f386ab0e9@warehouse-f80fbbe8e8.iam.gserviceaccount.com`.
GCP already uses that identity and the warehouse source in its live schedule.
Do not enable self-impersonation in those pods. Local ADC validation can
explicitly opt into impersonation with a billing query project; it is not the
production identity. A fresh dedicated-KSA runtime canary remains a separate
manual rollout check, not a completed test claimed by this design.

Resolved decisions:

- Kubernetes residual allocation runs before EQ chargeback.
- All current-EQ-owned costs are chargeback eligible; anonymous costs are not
  inferred to be EQ-owned.
- Label enrichment is preferred over blindly allocating costs with incomplete
  labels.
- Budget readers use the current membership schema; unsupported repo/group/label
  scopes are not implicitly interpreted as unrestricted account budgets.

## References

- [Google Cloud Billing BigQuery export overview](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery)
- [Google Cloud Billing export table types](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables)
- [Google Cloud Detailed usage cost export schema](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/detailed-usage)
- [Google Cloud Billing APIs overview](https://docs.cloud.google.com/billing/docs/apis)
