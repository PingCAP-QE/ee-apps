# Unified Cost Allocation and EQ Chargeback Design

Status: Implemented in code; migration and production rollout pending
Date: 2026-08-21

Implementation note: migration `016_add_materialized_cost_allocations.sql`,
`materialize-cost-allocations`, and the four Dashboard basis choices implement
the GCP-native and EQ stages. AWS keeps its existing split-cost adapter and
parent-residual ledger; fully reconciled source lineage is materialized when
available, while broader AWS common-ledger cleanup remains Phase 7.

## Summary

Build one materialized cost-allocation pipeline with two ordered stages:

1. allocate provider-native Kubernetes residual cost;
2. allocate every remaining cost owned by the current Efficiency & Quality
   department to non-EQ groups.

GCP and AWS keep provider-specific source adapters, but normalize into the same
Kubernetes direct/residual model. The residual allocator and amount-conservation
logic are vendor-neutral. The EQ stage then operates on either native cost or
the Kubernetes-allocated result.

The Dashboard exposes one four-choice basis selector, equivalent to two
independent Boolean policies:

```text
allocate_kubernetes_residual = true | false
allocate_eq_cost = true | false
```

It selects one of four precomputed perspectives:

| K8s residual | EQ chargeback | Perspective |
| --- | --- | --- |
| off | off | native |
| on | off | kubernetes_allocated |
| off | on | eq_allocated |
| on | on | kubernetes_eq_allocated |

Allocation is calculated daily. Dashboard allocation views support weekly and
monthly aggregation only; they do not calculate weights for arbitrary request
ranges.

The target flow is:

```text
GCP Detailed Billing Export ── GCP adapter ──┐
                                             ├─ Kubernetes source facts
AWS CUR / Split Cost       ── AWS adapter ──┘
                                                        │
                                      Stage 1: K8s residual allocator
                                                        │
                         ┌──────────────────────────────┴──────────────┐
                         │                                             │
                   native basis                              K8s-allocated basis
                         │                                             │
                         └────────── Stage 2: EQ chargeback ──────────┘
                                                        │
                                           materialized cost perspectives
                                                        │
                                         Dashboard weekly/monthly queries
```

The API chooses a perspective and aggregates it. It does not identify the EQ
department, calculate participants or weights, handle rounding, or dynamically
replace source rows.

## Confirmed Decisions

- GKE Cost Allocation in Cloud Billing Detailed Export is the GCP Kubernetes
  source of truth.
- GCP usage metering is removed after native Cost Allocation cutover.
- GCP and AWS normalize into one Kubernetes source-fact contract.
- Kubernetes residual allocation always runs before EQ chargeback.
- Every cost whose current owner belongs to Efficiency & Quality is eligible
  for EQ chargeback.
- Department membership uses the current roster and is intentionally applied
  retroactively when historical dates are rebuilt.
- EQ allocation is daily and isolated by `(usage_date, vendor, account_id)`.
- Participant weights use positive native direct `list_cost` only.
- Allocation outputs from K8s or EQ stages never enter the EQ denominator.
- The same weight allocates list, effective, credit, and net cost.
- A day/account with no eligible non-EQ direct list cost retains its EQ cost.
- V1 targets the current direct `group_id`; it does not model leaf/parent
  hierarchy or duplicate results at multiple organization levels.
- Derived rows also preserve `manager_id` so existing manager budget views do
  not lose a dimension when a chargeback perspective is selected.
- TCMS `shared_pool` is a source/display label, not an allocation policy.
- Current TCMS owner/service/project matching remains, but
  `shared_weighted` pool redistribution is retired.
- Weekly and monthly Dashboard views aggregate materialized daily facts.

## Motivation

### Native GKE allocation already exists

The current GCP implementation combines GKE node list cost with CPU and memory
usage from `pingcap_ee_data.gke_cluster_resource_usage`. That assumption is
stale for `prow`: native GKE Cost Allocation is enabled and Detailed Billing
Export already contains request-based workload costs, Kubernetes labels,
explicit system and idle residuals, and supported Persistent Disk allocation.

Reallocating full node cost by metered consumption:

- redistributes costs already assigned by Google;
- replaces request-based accounting with consumption-based accounting;
- omits native GPU and supported Persistent Disk allocation;
- depends on a second BigQuery dataset; and
- makes Cloud Billing reconciliation harder.

### Infra ownership is not final consumption

Efficiency & Quality operates shared AWS and GCP infrastructure. Native cost
attribution therefore shows substantial cost under EQ even when other groups
consume the service. The Dashboard must show both:

```text
who currently owns/operates the source cost
who bears the cost after chargeback
```

Because budgets and cost-saving responsibility follow the current
organization, historical cost is deliberately recalculated with the current
roster after a reorganization.

### API calculation does not scale as the semantic layer

The current Dashboard contains dynamic SQL that replaces reconciled Kubernetes
source rows with allocation facts. Cost attribution also materializes a
separate TCMS shared-pool allocation inside its refresh SQL. Extending both
patterns would produce four combinations of increasingly complex API CTEs.

Daily materialization provides:

- predictable API latency;
- explicit policy and roster versions;
- deterministic rounding;
- persisted source and target lineage;
- amount-conservation checks before publication; and
- one definition reused by every endpoint.

## Validated GCP Evidence

Read-only validation was run on 2026-08-21 against project
`pingcap-testing-account`.

### Cluster configuration

```yaml
name: prow
location: us-central1-c
status: RUNNING
costManagementConfig:
  enabled: true
```

`prow` was the only listed GKE cluster.

### Billing export

The authoritative source is:

```text
gcp-digital-bi.gcp_billing_detailed.
gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6
```

The latest observed partition was `2026-08-21`.

For partition `2026-08-19`, the export contained:

| Dimension | Observed result |
| --- | ---: |
| cluster | `prow` |
| distinct namespaces | 29 |
| distinct workload types | 6 |
| distinct workload names | 6,198 |
| direct rows with workload name | 94.7% |
| distinct `core/v1-Pod` workload names | 6,130 |

Observed labels included author, org, repo, target branch, Prow job, Prow ID,
and build ID. Supported Hyperdisk, Balanced PD, and SSD-backed PD costs also
carried direct workload metadata.

Observed cluster-labelled net cost was:

| Class | Net cost | Share |
| --- | ---: | ---: |
| direct workload | $198.93 | 54.0% |
| idle | $94.87 | 25.8% |
| unsupported | $39.59 | 10.7% |
| unclassified | $18.38 | 5.0% |
| system overhead | $9.86 | 2.7% |
| unknown | $4.51 | 1.2% |
| control plane | $2.21 | 0.6% |

Native direct rows provide workload identities and request-based participant
weights; the remaining rows form explicit residual pools.

## Goals

1. Preserve provider-native direct Kubernetes allocations.
2. Allocate supported Kubernetes residual with one common algorithm.
3. Reassign all remaining current-EQ-owned cost to non-EQ groups when a daily
   denominator exists.
4. Preserve native ownership and chargeback ownership separately.
5. Materialize all derived perspectives before serving them.
6. Conserve each available cost amount by source, allocation group, day,
   vendor, and account.
7. Keep unallocatable cost visible.
8. Recalculate historical EQ eligibility using the current roster.
9. Remove GKE usage metering and TCMS shared-pool weighting.
10. Keep existing operational CLI names during rollout.

## Non-goals

- Historical organization snapshots or as-of-date roster reconstruction.
- Arbitrary user-selected allocation windows.
- Daily Dashboard allocation views.
- Parent/leaf organization rollups in V1.
- Allocating unattributed cost to EQ merely because it is in an EQ-managed
  cloud account.
- Using `shared_pool` as an EQ chargeback boundary.
- Container-level cost below provider Pod/workload grain.
- Retiring the AWS parent residual audit table during GCP cutover.

## Domain Model

### Native fact

A row in `cost_attribution_daily` before derived allocation. It retains source
service/SKU, labels, current owner attribution, and all four cost amounts.

### Source owner

The employee/group currently resolved from provider labels, TCMS metadata, and
roster. Rebuilding historical dates after a reorganization may change this
owner by design.

### Charged group

The current direct roster group that bears a derived cost. V1 stores only the
direct `group_id`; it does not copy the same cost to parent groups.

### Kubernetes source fact

A provider billing fact normalized as Kubernetes `direct` or `residual` before
internal residual redistribution.

### Kubernetes direct cost

Cost assigned by GCP Cost Allocation or AWS split-cost data to a concrete
namespace/workload. It passes through unchanged.

### Kubernetes residual cost

Kubernetes-related cost that the provider did not assign to a concrete
workload. It has one normalized residual type.

### Allocation group

The smallest boundary in which residual can use direct participants.
`allocation_group_hash` persists that identity. Provider adapters create the
boundary; the common allocator does not branch on vendor.

### Participant basis

A positive native direct list-cost amount used only to calculate a weight.
Derived allocation facts never become participant basis.

### Basis key

The serving-perspective identifier, not a participant or denominator hash. Its
only allowed values in `cost_allocation_daily` are:

```text
kubernetes_allocated
eq_allocated
kubernetes_eq_allocated
```

Native has no `basis_key` row because it remains in `cost_attribution_daily`.

### Materialized perspective

A complete, non-overlapping daily cost view for one switch combination.
Derived perspectives replace eligible source facts; they are never added on
top of the source facts they replace.

## Ordered Allocation Stages

### Stage 0: native attribution

Refresh `cost_attribution_daily` from billing summary and current roster.
Ordinary TCMS tag matching may resolve owner/service/project dimensions, but no
TCMS shared-pool cost weighting occurs.

### Stage 1: Kubernetes residual allocation

For each provider-normalized Kubernetes group:

1. pass direct workload facts through;
2. redistribute policy-supported residual to matching direct workloads;
3. retain residual with no eligible participant or policy;
4. conserve every available amount.

This stage produces `kubernetes_allocated`.

### Stage 2: EQ chargeback

Run independently against two inputs:

```text
native                  → eq_allocated
kubernetes_allocated    → kubernetes_eq_allocated
```

For each input row:

- if its current charged group is outside EQ, pass it through;
- if it belongs to EQ and a daily denominator exists, replace it with one row
  per eligible non-EQ group;
- if it belongs to EQ and no denominator exists, retain it under EQ with an
  explicit unallocated method.

When both switches are enabled, Kubernetes runs first. Kubernetes cost already
charged to a non-EQ group is not processed again. Kubernetes cost that remains
charged to EQ enters Stage 2 like any other EQ cost.

## Kubernetes Provider Normalization

### Common source contract

Provider adapters emit:

```text
usage_date
vendor / account_id
cluster_name / cluster_location
allocation_group_hash
source_fact_hash
source_summary_row_hash
provider_scope
source_cost_class        direct | residual
residual_type
cost_component
service_name / sku_name / usage_type
parent_resource_name
namespace / workload_name / workload_type
author / org / repo / target_branch
allocation_basis
list_cost / effective_cost / credit_amount / net_cost
source_export_time
allocation_version
```

Provider-specific source fields may remain in `vendor_tags_json`, but common
allocation code does not inspect them.

### Common Kubernetes scopes

Final Kubernetes facts use:

| Scope | Meaning |
| --- | --- |
| `direct` | unchanged provider-assigned workload cost |
| `residual_redistributed` | residual assigned to a workload |
| `residual_unallocated` | residual retained without a participant/policy |

### GCP classification

A GKE source row has nonempty `goog-k8s-cluster-name`.

Classification is evaluated in this order:

| Source condition | Class | Residual type |
| --- | --- | --- |
| normal namespace and workload | direct | `NULL` |
| `kube:system-overhead` | residual | `system_overhead` |
| `kube:unallocated` | residual | `idle` |
| `goog-k8s-unknown` | residual | `unknown` |
| `goog-k8s-unsupported-sku` | residual | `unsupported` |
| Kubernetes Engine without workload | residual | `control_plane` |
| cluster label with missing namespace/workload | residual | `unclassified` |

Cost components are:

```text
cpu
memory
gpu
storage
control_plane
network
other
```

The billing summary uses the workload name as `resource_name` for GKE direct
facts and leaves it null for GKE residual facts. Underlying node, disk, and
other provider resource IDs stay in the unmatched-resource investigation feed;
they must not multiply one workload or residual summary fact into thousands of
resource-level rows.

The initial GCP allocation group is:

```text
usage_date
vendor = gcp
account_id
cluster_name
cluster_location
service_name
sku_name
cost_component
allocation_version
```

Exact SKU grouping prevents cross-pricing-class subsidy. A residual SKU with no
matching direct participant stays unallocated.

### AWS classification

| AWS source | Class | Residual type |
| --- | --- | --- |
| `eks_pod` | direct | `NULL` |
| Kubernetes `split_child` with workload | direct | `NULL` |
| parent residual | residual | `parent_residual` |
| `eks_unallocated` | residual | `idle` |
| direct AmazonEKS control plane | residual | `control_plane` |
| cluster-adjacent row without workload | residual | `unclassified` |

The initial AWS allocation group is:

```text
usage_date
vendor = aws
account_id
parent_resource_id
cost_component
allocation_version
```

Direct pod split list cost supplies participant basis.

### Initial Kubernetes residual policy

| Residual type | Action |
| --- | --- |
| `system_overhead` | redistribute |
| `idle` | redistribute |
| `parent_residual` | redistribute |
| `control_plane` | retain unallocated |
| `unsupported` | retain unallocated |
| `unknown` | retain unallocated |
| `unclassified` | retain unallocated |

Policy changes require a new version and rebuild.

## Common Residual Allocator

The allocator contains no vendor branch. Provider adapters intentionally define
different group boundaries; common code guarantees equivalent allocator
behavior after those boundaries have been normalized. For one group:

```text
D = sum(positive direct allocation_basis)
weight_i = participant_basis_i / D
allocated_amount_i = residual_amount × weight_i
```

Rules:

1. publish direct facts unchanged;
2. exclude zero/negative basis from participants;
3. apply the same weight to all available cost amounts;
4. keep an unavailable provider amount as `NULL`;
5. quantize weights to 16 decimals and currency to cents;
6. give the deterministic final participant the remaining weight and cents;
7. retain residual when `D = 0` or policy forbids redistribution.

## EQ Chargeback

### EQ identity

Configure one stable Lark department identifier:

```text
COST_INSIGHT_EQ_ROOT_LARK_GROUP_ID
```

At job start, resolve it to the current `roster_groups.id`. The existing roster
schema stores ID paths such as `/1/4/9/`, not group-name paths. A group belongs
to EQ when its current path contains the delimiter-safe segment
`/<eq_root_id>/`:

```sql
group_path LIKE CONCAT('%/', :eq_root_id, '/%')
```

The predicate applies to the current path copied to `roster_employees` or to
`roster_groups.path`; it must not use an unbounded substring or group name.
Current direct group membership is sufficient; V1 does not model parent
reporting dimensions.

The run records:

```text
eq_root_lark_group_id
roster_resolved_at
allocation_version
```

### Eligible EQ source cost

Every input fact whose current charged `group_id` belongs to the active EQ
department tree is eligible. This includes all services and all cost
components. No service, resource, or `shared_pool` allowlist is required.

A cost with no resolved group is not inferred to be EQ. It stays unattributed.

### Daily isolation boundary

EQ allocation never crosses:

```text
usage_date
vendor
account_id
```

AWS account cost is allocated only to groups with direct cost in that AWS
account on that date. GCP project cost follows the same rule. Vendors and
accounts never subsidize one another.

### Participants

A participant is a current non-EQ direct `group_id` with positive native direct
list cost in the same daily boundary.

The denominator always reads Stage 0 native direct facts. It excludes:

- EQ-owned source facts;
- Kubernetes residual redistributed or unallocated facts;
- prior EQ chargeback output;
- all other derived allocation output;
- zero/negative list cost; and
- unattributed rows without `group_id`.

Using one fixed native denominator prevents loops and keeps EQ weights unchanged
when the Kubernetes switch changes.

For group `g`:

```text
basis_g = sum(positive native direct list_cost for group g)
D = sum(basis_g for all current non-EQ groups)
weight_g = basis_g / D
```

### EQ source allocation

For each EQ-owned input amount `C`:

```text
allocated_to_g = C × weight_g
```

The same group weight is applied to list, effective, credit, and net cost. The
final deterministic group receives the rounding remainder. Credits remain
signed, so a credit-only EQ row can produce a negative target `credit_amount`
or `net_cost`. That is accepted: each source amount is conserved and the UI
must display negative chargeback amounts rather than clamp them to zero.

If `D = 0`, the source fact is retained under EQ with:

```text
allocation_scope = residual_unallocated
allocation_method = eq_no_non_eq_direct_cost
```

There is no cross-day or cross-account fallback.

### Reorganization behavior

This design intentionally uses current organization membership. If a group or
employee moves during a reorganization, rebuilding a historical range may
change:

- whether source cost is considered EQ-owned;
- which direct group receives participant basis; and
- the resulting historical chargeback.

This is desired because budgets and cost-saving responsibility follow the
current organization. No effective-dated roster snapshot is required.

Any semantic roster change that can affect allocation marks derived
perspectives stale. This includes employee identity/group/manager/active-state
changes and group parent/path/manager/active-state changes; sync bookkeeping
such as `last_seen_at` or `updated_at` alone does not. The allocation job
rebuilds the complete configured window passed as the job's required
`--start-date` / `--end-date` using the new current roster, then publishes a new
version atomically. Deployment sets the stable earliest date after measuring
the intended Dashboard history and rebuild volume; there is no implicit default
or rolling-window fallback. Derived perspectives are
unavailable before the configured date rather than being served with a stale
roster version.

## TCMS Interaction

TCMS continues to provide source metadata:

```text
owner
service
project
service_exec_id
vendor_tags_json.shared_pool
vendor_tags_json.cluster
```

The existing most-specific tag match remains useful for attributing a billing
row to an owner or business dimension.

The special shared-pool weighting path is not part of the target model:

```text
allocate_method = shared_weighted
```

`allocate_method` is the persisted legacy column on `cost_attribution_daily`
from migration `006`; `allocation_method` is the canonical column on Kubernetes
and new derived ledgers. V1 keeps both schema names to avoid an unrelated
attribution migration. New code must not add another spelling.

Removing `shared_weighted` intentionally rebuilds affected attribution rows and
therefore changes their `dimension_hash`, because the method and often the
service/project dimensions change. Attribution refresh already replaces the
full vendor/account/date range transactionally; shadow validation must compare
source totals and changed dimensions before cutover.

Current `shared_weighted` output has `owner`, `group_id`, and `manager_id` set to
`NULL`; it does not make a pool EQ-owned. After retirement, author-less pool
cost remains unattributed and is not EQ-chargeback eligible. Its
`vendor_tags_json.shared_pool` and total cost must remain visible so TCMS users
can still inspect the pool. Ordinary TCMS matches that resolve an owner continue
to work. `shared_pool` does not define an allocation group or participant
denominator.

## Persistence Model

### Billing summary

`cost_bq_export_summary_daily` remains the normalized billing ledger. Add
nullable common Kubernetes fields:

```sql
cluster_name VARCHAR(255) NULL,
cluster_location VARCHAR(128) NULL,
kubernetes_cost_class VARCHAR(32) NULL,
kubernetes_residual_type VARCHAR(32) NULL,
kubernetes_cost_component VARCHAR(32) NULL
```

`NULL` means the row is outside the Kubernetes allocation model. Existing
`source_allocation_scope` remains provider/audit metadata.

GCP summary identity includes native Kubernetes dimensions only for recognized
GKE Cost Allocation rows. Ordinary non-Kubernetes GCP hashes remain unchanged.

### Kubernetes source ledger

Evolve `cost_kubernetes_workload_allocation_source_daily` into a common GCP/AWS
source ledger. Add normalized class, residual, component, workload, amount, and
provider-independent source identity fields.

The new identity is enforced exactly as:

```sql
UNIQUE KEY uk_cost_kubernetes_allocation_source_fact (
  usage_date,
  vendor,
  account_id,
  source_fact_hash
)
```

`source_fact_hash` is always non-null. `source_summary_row_hash` may become
nullable for provider facts without a summary-row identity, remains indexed for
Dashboard lineage, and is not used as the sole uniqueness constraint.

### Kubernetes final ledger

Continue using `cost_kubernetes_workload_allocation_daily` for auditable Stage 1
facts. Add canonical source class, residual type, provider scope, and generic
source/final cost amounts. Keep legacy `source_node_list_cost` during
compatibility rollout but stop using it in common code.

### Materialized serving perspectives

Add `cost_allocation_daily` for the three derived perspectives:

```text
kubernetes_allocated
eq_allocated
kubernetes_eq_allocated
```

Native continues to read `cost_attribution_daily`; it is not copied.

The derived table mirrors the serving dimensions needed by Dashboard and adds:

```text
basis_key
allocation_stage
source_fact_hash
source_owner
source_group_id
source_manager_id
target_group_id
target_manager_id
allocation_scope
allocation_method
allocation_weight
allocation_version
roster_resolved_at
list_cost
effective_cost
credit_amount
net_cost
dimension_hash
```

`basis_key` stores one literal perspective name from the enum defined above. It
is coarser than `allocation_stage`—one perspective may contain multiple stages—and
exists so Dashboard can select a complete perspective with one equality filter.

For EQ redistribution, `target_manager_id` is the current
`roster_groups.manager_id` of the target group. Pass-through rows preserve their
existing manager. Only one target `group_id` and `manager_id` pair is stored for
V1; parent groups are not materialized.

Rows remain daily even though Dashboard exposes weekly/monthly allocation
views. This avoids allocation-week boundaries crossing calendar months. Staged
and active versions coexist under:

```sql
UNIQUE KEY uk_cost_allocation_daily_versioned (
  basis_key,
  allocation_version,
  usage_date,
  dimension_hash
)
```

Add `cost_allocation_publication` as the publication pointer:

```text
publication_name              primary key; `dashboard`
active_allocation_version     one completed version
updated_at
```

Derived API queries resolve that pointer and filter by both `basis_key` and
`allocation_version`; they never infer the active version with `MAX()`.

### Idempotency

A successful run builds all requested daily perspectives and conservation
reports before publication. The calculation and persistence chunk is one
`(vendor, account_id, usage_date)` window, not the entire historical range in
one transaction. A full roster-triggered rebuild stages every daily chunk under
one new `allocation_version`; the API continues reading the prior active
version until all chunks pass conservation, then one metadata-pointer update
activates the new version atomically. Old versions may be removed afterward.

Publication always rebuilds the complete configured history: `start_date` must
equal `COST_ALLOCATION_EARLIEST_DATE`, and `end_date` must cover the latest
native fact. This prevents a partial version from hiding dates behind the one
global publication pointer. An account/date with no native facts intentionally
has no derived rows and contributes zero; it does not inherit stale rows from
an older version. A nonempty partition is publishable only after the source
import's partition-completeness gate passes; a partial or incomplete partition
aborts without replacing the previous perspective.

One input fact may appear once in each complete perspective, but never twice
inside one perspective. The derived `dimension_hash` includes the input fact's
stable hash plus its allocation target, so two allocation facts with identical
visible workload labels remain distinct.

## Conservation Contracts

### Kubernetes source normalization

For each provider/day/account and available amount:

```text
normalized K8s direct + normalized K8s residual
= recognized provider Kubernetes total
```

### Kubernetes allocation group

```text
source direct + source residual
= final direct + residual_redistributed + residual_unallocated
```

### EQ daily group

For each day/vendor/account and each perspective input:

```text
non-EQ pass-through + EQ source
= non-EQ pass-through + EQ redistributed + EQ retained
```

Equivalently:

```text
sum(input amount) = sum(output amount)
```

### Perspective completeness

Before publication:

- every replaced source fact resolves once;
- every allocation group is complete;
- each available amount agrees to the cent;
- no source uses two visible policy versions; and
- all three derived perspectives cover the same native date window.

A failed contract leaves the previous production window intact.

## Dashboard API

### Parameters

The implementation reuses the existing `allocation_basis` parameter instead of
adding two redundant request parameters:

```text
allocation_basis:
  current_attribution
  residual_allocated
  eq_allocated
  residual_eq_allocated
granularity: week | month
```

These four values map directly to the two policy Booleans.

Mapping:

| K8s | EQ | Source |
| --- | --- | --- |
| false | false | `cost_attribution_daily` |
| true | false | `cost_allocation_daily`, `kubernetes_allocated` |
| false | true | `cost_allocation_daily`, `eq_allocated` |
| true | true | `cost_allocation_daily`, `kubernetes_eq_allocated` |

Every derived query also filters on the publication record's single active
`allocation_version`.

The API rejects daily allocation granularity and arbitrary custom grouping
windows for these comparison views. Existing native detail endpoints may remain
daily where they do not claim to show chargeback.

The switches are exposed only after native GKE Cost Allocation is the Stage-0
source. Therefore `eq_allocated` always means EQ chargeback over provider-native
cost with K8s residual retained; it never means EQ chargeback over legacy GKE
metering output.

### Aggregation

Weekly and monthly views perform ordinary aggregation only:

```sql
SUM(list_cost)
SUM(effective_cost)
SUM(credit_amount)
SUM(net_cost)
GROUP BY week/month and requested dimension
```

Weekly display still needs a UI convention such as Monday through Sunday, but
that convention does not affect daily allocation weights.

### Effect reporting

Dashboard should make the perspective explicit and may show:

```text
native cost
K8s residual allocated in/out
EQ cost allocated out
cost received from EQ
EQ cost retained because no denominator exists
final charged cost
```

Source owner and charged group must remain separately inspectable.

## Rollout Plan

### Phase 0: baseline

1. Capture GKE Cost Allocation enabled state and representative source totals.
2. Capture current `gke_metering_v4` and K8s Dashboard totals.
3. Capture AWS parent/day split and residual totals.
4. Resolve the stable EQ root `lark_group_id` and set the deployment's required
   allocation `--start-date` after measuring the intended Dashboard history and
   full-window rebuild volume.
5. Capture current EQ native cost and non-EQ direct list-cost denominators by
   day/vendor/account.
6. Quantify the count and fraction of EQ cost retained because the daily
   denominator is zero, by vendor/account.
7. Capture current TCMS `shared_weighted` totals and dimensions, including the
   fraction with non-null owner/group/manager, for rollback comparison.

### Phase 1: common schema and pure allocation logic

1. Add common Kubernetes columns and ledgers in the next migration after `015`.
2. Add `cost_allocation_daily` and the one-row
   `cost_allocation_publication` pointer.
3. Implement provider-normalized source facts.
4. Implement one vendor-neutral residual allocator.
5. Implement one daily EQ allocator using direct group IDs.
6. Add conservation reports and deterministic rounding tests.

### Phase 2: GCP native shadow

Run the native GCP adapter for representative dates without serving it. Require:

- exact source classification totals;
- direct/residual coverage by component;
- every allocation-group delta within one cent;
- no duplicate source replacement; and
- native PD workload metadata retained.

### Phase 3: GCP Kubernetes cutover

1. Re-import matching GCP billing summary partitions with
   `--replace-existing-partitions`: spool and validate the complete replacement,
   delete all existing rows in each vendor/account/export-partition range, then
   insert. This intentionally removes old-hash GKE rows before the new
   Kubernetes-aware summary hashes are inserted.
2. Publish normalized GCP K8s source/final facts.
3. Refresh attribution for the same dates.
4. Build `kubernetes_allocated`.
5. Smoke-test total, workload, repo, author, storage, and residual views.
6. Keep the existing CLI command name.

Rollback redeploys the prior image and rebuilds the bounded window with
`gke_metering_v4`.

### Phase 4: EQ shadow

For a representative historical range, build `eq_allocated` and
`kubernetes_eq_allocated` without serving them. Report:

- native EQ cost by day/vendor/account;
- participant basis and weight by direct group;
- redistributed and retained EQ cost;
- count and percentage retained specifically because `D = 0`, by
  day/vendor/account, with no cross-day fallback;
- reorganization-sensitive source/target mappings;
- each perspective's conservation delta; and
- difference from the old TCMS `shared_weighted` view.

### Phase 5: Dashboard cutover

1. Publish all three derived perspectives for the same complete window.
2. Extend the existing basis selector to the four policy combinations and keep
   the week/month restriction.
3. Compare all four perspectives for matching totals.
4. Remove dynamic allocation arithmetic from API queries after equivalent
   materialized results are verified.
5. Stop serving TCMS `shared_weighted` output.

### Phase 6: remove obsolete paths

After at least seven healthy days:

- remove `GcpBillingSettings.gke_usage_table` and environment aliases;
- delete the GKE metering fetch/query and workload-usage model;
- remove metering-only tests;
- remove TCMS shared-pool weighting SQL and tests;
- add a CI grep guard proving no source/job references
  `gke_cluster_resource_usage` or `GcpBillingSettings.gke_usage_table`;
- keep ordinary TCMS owner/tag matching;
- update README and system design;
- mark `gke-workload-allocation-design.md` superseded;
- update `cost-data-flow.drawio` and PNG; and
- verify `ee-ops` contains no metering table override.

### Phase 7: AWS common-engine cutover

1. Shadow AWS normalized inputs against the existing parent residual ledger.
2. Require exact parent/day list-cost conservation.
3. Switch AWS Kubernetes facts to canonical scopes and generic amount columns.
4. Keep the provider audit ledger for one observation window.
5. Consider audit-ledger retirement only in a later cleanup.

## Code Change Map

Expected Cost Insight files:

- `src/cost_insight/sources/gcp_billing_export.py`
- `src/cost_insight/sources/gcp_gke_allocation.py`
- `src/cost_insight/jobs/sync_gcp_billing_summary.py`
- `src/cost_insight/jobs/sync_gcp_kubernetes_workload_allocations.py`
- `src/cost_insight/jobs/sync_aws_kubernetes_workload_allocations.py`
- `src/cost_insight/jobs/refresh_attribution_daily.py`
- one small vendor-neutral allocation module
- one daily perspective materialization job
- `src/cost_insight/common/config.py`
- `src/cost_insight/jobs/cli.py`
- the next SQL migration after `sql/015_retire_legacy_cost_schema.sql`

Expected Dashboard files:

- `ci-dashboard/src/ci_dashboard/api/queries/cost.py`
- cost API models/routes for the four basis values
- frontend controls that restrict allocation comparison to week/month

Expected documentation:

- `docs/gke-workload-allocation-design.md`
- `docs/label-allocation-design.md`
- `docs/system-design.md`
- `docs/cost-data-flow.drawio`
- `docs/cost-data-flow.drawio.png`
- `README.md`

## Test Plan

### Kubernetes adapters

- GCP direct and every residual classification;
- GCP CPU, memory, GPU, storage, network, and unknown components;
- native author/org/repo/branch labels;
- AWS provider-scope mapping and parent group identity;
- stable source and allocation-group hashes.

### Common residual allocator

- direct pass-through;
- supported residual redistribution;
- unsupported/no-participant retention;
- deterministic weight and cent remainder;
- all available amounts conserve independently;
- equivalent normalized GCP/AWS fixtures produce equivalent allocator behavior
  given each adapter's group definition;
- no vendor branch exists in allocator.

### EQ allocator

- current EQ root and descendants are eligible;
- non-EQ and unattributed source cost passes through;
- groups are keyed by direct `group_id` only;
- denominator is isolated by day/vendor/account;
- denominator uses positive native direct list cost only;
- K8s and EQ output never enters denominator;
- all EQ amounts use the same group weight;
- a credit-only negative row can produce negative target credit/net amounts
  while every source amount still conserves;
- zero denominator retains EQ cost and is included in shadow metrics;
- redistributed rows preserve the target group's current manager ID;
- K8s-first ordering leaves non-EQ K8s output untouched;
- K8s output still charged to EQ enters Stage 2;
- current-roster reorg changes historical rebuild results;
- no `shared_pool` weighting affects results.

### Persistence and serving

- a partial-history run cannot replace the global publication;
- an empty native account/date is published as zero without inheriting stale rows;
- rerun is idempotent;
- perspective replacement is atomic;
- each source appears once per perspective;
- all three derived perspectives cover the same dates;
- four switch combinations select the correct source;
- weekly/monthly sums equal daily materialized totals;
- incomplete source partitions never replace a published perspective;
- Phase 6 CI guard fails the merge if any source module or job references
  `gke_cluster_resource_usage` or `GcpBillingSettings.gke_usage_table`;
- no dynamic allocation arithmetic remains in the final API path.

## Acceptance Criteria

1. GCP production allocation no longer queries
   `gke_cluster_resource_usage`.
2. GCP native workload and supported PD metadata are preserved.
3. GCP and AWS use one normalized Kubernetes contract and residual allocator.
4. K8s residual always runs before EQ chargeback.
5. Every current-EQ-owned input fact is redistributed or explicitly retained.
6. EQ weights use only same-day, same-vendor/account, non-EQ positive native
   direct list cost.
7. V1 outputs direct target group and manager IDs without hierarchy
   duplication.
8. TCMS shared pool remains visible but never changes chargeback weights.
9. Every perspective conserves all available amounts.
10. Dashboard exposes all four combinations through its basis selector with
    weekly/monthly views.
11. API queries aggregate materialized facts instead of computing allocation.
12. Rebuilding history after reorg applies the current roster by design.

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| source and allocation output are both counted | complete perspective replacement with source lineage |
| K8s and EQ allocate one source twice | fixed Stage 1 then Stage 2 ordering |
| EQ weights become recursive | denominator reads native direct facts only |
| cross-account subsidy | isolate by date/vendor/account |
| no non-EQ usage exists on a day | retain EQ cost explicitly and report its fraction in shadow/operations |
| credits produce negative target amounts | preserve signed amounts, show them in the UI, and test conservation |
| storage residual uses CPU participants | group K8s by component and SKU |
| reorg changes historical results unexpectedly | show allocation version and roster resolution time; document retroactive behavior |
| group hierarchy complicates V1 | use direct group ID only |
| TCMS pool policy conflicts with EQ policy | remove `shared_weighted`; keep pool as metadata only |
| API becomes slower and harder to audit | materialize daily perspectives and aggregate only |
