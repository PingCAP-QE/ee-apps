# ADR 0004: OpenFGA topology

- Status: Proposed; pending platform/security review
- Date: 2026-11-24

## Context

The first rollout targets GCP. TencentCloud will require independent latency,
availability, trust-domain and data-residency validation. Prod2 is excluded.

## Recommendation

Use one private GCP OpenFGA deployment and store for the GCP pilot, with pinned
model IDs and a durable supported database/backup strategy. Do not expose its
API or management endpoint publicly; only dedicated sync/admin workloads may
write tuples. Evaluate a separate TencentCloud deployment after GCP evidence,
using the same model but a separate store and trust domain unless measured
requirements justify another topology. No cross-cloud or Prod2 store sharing.

## Consequences

This is a proposed topology, not a deployment authorization. Storage engine,
backup/restore, model publication, and regional placement remain platform review
items. No cluster resources are changed by this ADR.

## Approval

Pending: platform owner and security owner.
