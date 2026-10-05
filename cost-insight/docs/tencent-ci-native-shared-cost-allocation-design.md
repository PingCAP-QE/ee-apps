# Tencent CI shared-cost allocation

## Scope

This is a deliberately coarse allocation for only `tencent/100050658403`.
It writes the serving result directly to `cost_attribution_daily`; there are no
Tencent-specific tables, versions, snapshots, manifests, publishers, stale
state, or rollback workflow.

Tencent billing import remains independent. It never runs allocation.

## Source metadata and direct rows

The Tencent importer stores the provider `ProductCode` in
`vendor_tags_json.__tencent_product_code`. It preserves the complete billing-time
label object and maps labels into the common Project dimension with:

```python
project = tags.get("project") or tags.get("service")
```

An explicit `project` label therefore wins, while the original `service` label
remains available in `vendor_tags_json` and the common `service` field. Historical
billing rows must be reimported before allocation. If any source row for a day
lacks ProductCode, the allocation fails before it changes that day.

A ProductCode beginning with `sp_eks_supernode` is a supernode. Supernode rows
keep ordinary direct-summary attribution and never contribute to the shared
pool or build-weight denominator. Every other Tencent account cost, including
owner/author-tagged, storage, network, and COS rows, is shared.

## V1 formula

For each Beijing day, completed `ci_l1_builds` with `cloud_phase='TENCENT'`
are grouped by `(active roster employee, org, repo)`. A build has weight:

```
1 + max(run_seconds, total_seconds, 0) / 3600
```

Every matched employee/repo group and unmatched `(org, repo)` group receives
its proportional share of each `(currency, cloud service, service, project)`
pool. Unmatched rows have `owner`, employee, group, and manager fields set to
null. If there are no builds, each shared pool becomes one null-dimension
residual. Shared attribution rows preserve cloud service, service, and Project
instead of replacing them with a synthetic `Tencent CI shared` service.
Monetary splits round deterministically to nine decimals, with the final
participant absorbing the remainder; all-null amount fields remain null and
List, Effective, Credit, and Net totals are independently conserved.

## Resource serving

The shared attribution table stays compact: it stores one row per build
participant and source-dimension pool, not a resource × participant cross
product. During resource-serving materialization, the same daily participant
proportions are projected back onto every non-supernode summary resource.
Supernodes continue to use their direct `source_summary_row_hash` lineage.

Both paths publish through the existing `cost_resource_serving_daily` schema:
provider Resource ID, cloud service, billing-time labels, Project, owner/team,
and all four amount fields are preserved. A resource can therefore have
`list_cost=0` and positive `net_cost`, as Tencent contract-price COS billing
legitimately does. Region and SKU are intentionally outside this minimal scope
because the common serving table has no such columns.

## Job and replay semantics

Run one command:

```bash
cost-insight allocate-tencent-ci-cost --start-date YYYY-MM-DD --end-date YYYY-MM-DD [--dry-run]
```

Each day is one transaction: validate metadata, replace that account/day's
attribution with the existing direct-summary INSERT, delete non-supernode direct
source hashes, then insert the weighted shared rows. A failure rolls back that
day. Re-running recomputes and replaces the same projection. After each
successful day, the existing resource-serving materializer refreshes the
published resource view from summary lineage and the new attribution rows.

The generic summary refresh refuses this exact source, and generic cost
materialization excludes it, preventing a second allocation.

## Schedule

Run daily after the Tencent D+3 import completes, for the imported Beijing
billing day. Use explicit ranges for reimported history or repairs.

## Deferred V2

CPU/memory job profiles are V2 documentation-only work. They are not stored or
used by V1.
