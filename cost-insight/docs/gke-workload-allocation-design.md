# GKE Workload Cost Allocation Design

## Context

GKE node costs in the billing export identify a cluster but not the workload
that consumed the node's CPU or memory. The GKE metering table contains those
workload dimensions. This job writes an auditable allocation fact table instead
of changing the source billing rows or inventing ownership for shared cost.

## Scope

The job writes `cost_kubernetes_workload_allocation_daily` and
`cost_kubernetes_workload_allocation_source_daily` for one GCP account and a
bounded usage-date range. Allocation facts use one scope:

| Scope | Meaning |
| --- | --- |
| `workload_split` | A recognized CPU or memory node cost allocated to a metered workload. |

The source mapping table keeps one source billing-summary row hash and list cost
per allocation group. The job does not allocate ordinary Compute Engine cost,
infer Kubernetes requests, or turn missing labels into an owner.

## Source Recognition

### Node cost

The billing-export query recognizes positive GKE signals:

- Compute Engine CPU and memory SKUs with a nonempty
  `goog-k8s-cluster-name` label.
- Compute Engine CPU and memory SKUs whose `resource.name` or
  `resource.global_name` identifies a `gke-` instance, even if the cluster
  label is missing.

Compute flexible committed-use discount adjustments are excluded so the source
list-cost basis matches the dashboard's list-cost calculation. Disks, network,
IP, Kubernetes control plane, and unidentifiable Compute Engine charges do not
enter this allocation job.

### Workload metering

`pingcap_ee_data.gke_cluster_resource_usage` is ingestion-time partitioned.
For a requested usage range the query applies both:

- `DATE(start_time)` between the requested dates, which defines the usage
  interval.
- `_PARTITIONDATE` from `usage_start_date` through one day after
  `usage_end_date`, which prunes the table while covering the observed
  next-partition arrival of usage rows.

Only positive `cpu` and `memory` usage participates. Metering is grouped by
usage date, cluster name, cluster location, namespace, workload identity, and
available Prow labels. Workload identity prefers `job-name`, then
`jenkins/label`, `prow.k8s.io/job`, application label, and finally namespace.

## Allocation

CPU and memory node components are allocated separately within the same
`(usage_date, cluster_name, cluster_location, cost_component)` group. The
component weight is the workload's metered CPU seconds or memory byte-seconds
divided by the group total. The job sums all eligible source costs in the group
before creating one allocation fact per workload, instead of materializing a
source-row by workload cross product.

Persisted weights use 16 decimal places. Costs use cents: each participant
except the deterministic final participant is rounded normally, and the final
participant receives the remaining cent value. The final persisted weight is
the remainder after the preceding quantized weights, so stored weights sum to
exactly one. `allocation_weight` is the normalized metering share, not an
assertion that `list_cost / source_node_list_cost` is exact after cent rounding.

## Cost Outside Allocation

The following remain as their ordinary direct-attribution rows; the job does
not write duplicate allocation facts for them:

- GKE node-adjacent components outside CPU and memory, including disks,
  network, IP, and PVC resources.
- CPU or memory components with no positive matching metering rows.
- Kubernetes Engine control-plane cost.

This is an explicit accounting outcome, not a diagnosis of why metering is
missing. In particular, the job does not have Pod request data and must not
claim that an unallocated workload lacks requests.

## Idempotency And Dashboard Read Path

Rows are keyed by `(usage_date, dimension_hash)`. A successful non-dry-run
replace deletes and rewrites only the requested account and usage-date window;
an empty recognized-node source is a no-op to avoid erasing a previous result.

Each source mapping links its exact GCP billing-summary row hash to an
allocation group. Dashboard allocation queries replace the original source rows
only when every mapping resolves to exactly one attribution row, the mapped and
attributed source list-cost totals agree, and the workload allocation total
also agrees. Unmatched or ambiguous rows remain as current attribution. This
prevents double counting while avoiding a source-row by workload cross product.

`target_branch` is a workload dimension. A branch-filtered allocated view
contains matching workload allocations; direct costs outside allocation retain
their original branch dimensions.

## Operations

Run the job after billing and metering data for the usage dates are stable:

```bash
cost-insight sync-gcp-kubernetes-workload-allocations \
  --usage-start-date 2026-08-10 \
  --usage-end-date 2026-08-10
```

`--export-partition-start` and `--export-partition-end` control the billing
export scan for node cost. The metering query uses its own ingestion partition
pruning derived from the usage-date window.

When enabling the dashboard's `K8S residual allocated` basis for historical
data, apply migrations `013_add_kubernetes_allocation_source_lineage.sql` and
`014_add_gke_allocation_group_lineage.sql` first. Then re-import the billing
summary, write the allocation facts and source mappings, and refresh attribution
in that order.
All commands must use the same billing-export partition window so the hashes
match:

```bash
cost-insight sync-gcp-billing-summary \
  --export-partition-start 2026-08-10 \
  --export-partition-end 2026-08-20 \
  --replace-existing-partitions

cost-insight sync-gcp-kubernetes-workload-allocations \
  --usage-start-date 2026-08-10 \
  --usage-end-date 2026-08-16 \
  --export-partition-start 2026-08-10 \
  --export-partition-end 2026-08-20

cost-insight refresh-cost-attribution-from-summary \
  --start-date 2026-08-10 \
  --end-date 2026-08-16 \
  --split-by-day
```
