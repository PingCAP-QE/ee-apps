# Tencent CI shared-cost allocation

## Scope

This is a deliberately coarse allocation for only `tencent/100050658403`.
It writes the serving result directly to `cost_attribution_daily`; there are no
Tencent-specific tables, versions, snapshots, manifests, publishers, stale
state, or rollback workflow.

Tencent billing import remains independent. It never runs allocation.

## Source metadata and direct rows

The Tencent importer stores the provider `ProductCode` in
`vendor_tags_json.__tencent_product_code`. Historical billing rows must be
reimported before allocation. If any source row for a day lacks this key, the
allocation fails before it changes that day.

A ProductCode beginning with `sp_eks_supernode` is a supernode. Supernode rows
keep ordinary direct-summary attribution and never contribute to the shared
pool or build-weight denominator. Every other Tencent account cost, including
owner/author-tagged, storage, network, and COS rows, is shared.

## V1 formula

For each Beijing day, completed `ci_l1_builds` with `cloud_phase='TENCENT'`
are grouped by active roster employee. A build has weight:

```
1 + max(run_seconds, total_seconds, 0) / 3600
```

Matched employee weights receive the daily non-supernode pool per currency.
Missing or unmatched authors are combined into one `owner=NULL` residual. If
there are no builds, the entire shared pool is residual. Shared rows have only
`service_name='Tencent CI shared'`; no repo, job, or source service dimension
is retained. Monetary splits round deterministically to nine decimals and leave
all-null amount fields null while conserving each day/currency total.

## Job and replay semantics

Run one command:

```bash
cost-insight allocate-tencent-ci-cost --start-date YYYY-MM-DD --end-date YYYY-MM-DD [--dry-run]
```

Each day is one transaction: validate metadata, replace that account/day's
attribution with the existing direct-summary INSERT, delete non-supernode direct
source hashes, then insert the weighted shared rows. A failure rolls back that
day. Re-running recomputes and replaces the same projection. After a successful
day, the existing resource-serving materializer may refresh its derived view.

The generic summary refresh refuses this exact source, and generic cost
materialization excludes it, preventing a second allocation.

## Schedule

Run daily after the Tencent D+3 import completes, for the imported Beijing
billing day. Use explicit ranges for reimported history or repairs.

## Deferred V2

CPU/memory job profiles are V2 documentation-only work. They are not stored or
used by V1.
