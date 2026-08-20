# Kubernetes Workload Cost Allocation

## Scope

The Cost page classifies Kubernetes-related list cost across AWS, GCP, and
Tencent Cloud. The two K8S metrics are independent of the normal owner cost
view: they show what has been allocated to workload use and what remains
unallocated after Kubernetes processing.

## Source Precedence

`cost_kubernetes_workload_allocation_daily` is authoritative for each
`(vendor, account_id, usage_date)` it covers. The Cost queries exclude legacy
`cost_attribution_daily` rows for those covered dates so a node, PVC, or
control-plane cost cannot be counted twice. Before the allocation table is
deployed, the same queries fall back to the legacy attribution rows.

## Classification

| Input | K8S allocated cost | K8S unallocated cost |
| --- | --- | --- |
| Workload or pod split | Yes | No |
| Node or PVC residual with an active employee match | Yes | No |
| Node or PVC residual without an active employee match | No | Yes |
| Control-plane cost with an active employee match | No | No |
| Control-plane cost without an active employee match | No | Yes |

An allocation-fact author matches only when it resolves to an active roster
employee by email or GitHub ID. Matched control-plane cost stays in the normal
owner cost view and is deliberately excluded from both K8S cards. This avoids
showing controller or master overhead as workload allocation while preserving
the owner-attributed spend in the primary cost views.

## Vendor Mapping

The allocation fact stores provider-neutral components. Audit API responses map
them to the provider billing-service names below:

| Vendor | Control plane | Node or residual |
| --- | --- | --- |
| AWS | `AmazonEKS` | `AmazonEC2` |
| GCP | `Kubernetes Engine` | `Compute Engine` |
| Tencent Cloud | `Tencent Kubernetes Engine` | `Cloud Virtual Machine` |

Unknown vendors remain explicitly marked as allocation facts rather than being
mislabelled as an AWS or GCP service.

## Audit APIs

`/api/v1/pages/cost-kubernetes-unallocated` and
`/api/v1/pages/cost-kubernetes-unallocated-records` remain available for
auditing and future tooling. They are intentionally API-only: the Cost-page
unallocated detail table was removed because its visible dimensions did not
provide useful operator context.
