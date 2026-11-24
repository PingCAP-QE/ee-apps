# ADR 0002: TiBuild read visibility

- Status: Proposed; decision intentionally deferred pending TiBuild product owner
- Date: 2026-11-24

## Context

TiBuild list endpoints can return multiple resources. A gateway allow/deny cannot
replace row filtering, and the current product visibility contract must be
understood before a single-resource `can_read` relation is enforced.

## Recommendation

Keep existing get/list behavior and data-layer filtering unchanged. Do not
enable resource-level read enforcement or infer that every build is owner-only.
Before expansion, the TiBuild owner must state whether get is globally visible,
creator-visible, project viewer-visible, or a combination, and separately
specify list filtering semantics. The model's `can_view` relation is illustrative
and must not be treated as an approved product policy.

## Consequences

The sample registry's get rule is an observe-only contract example, not a
production authorization decision. It must be removed or revised unless approved
read semantics and parity evidence are available.

## Approval

Pending: TiBuild product owner and security owner.
