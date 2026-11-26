# ADR 0005: TiBuild object ownership stays in TiBuild

- Status: Accepted (2026-11-26)
- Supersedes the earlier "proposed exception" version of this ADR

## Context

TiBuild's devbuild schema stores a build ID and an optional `createdBy` string.
There is no project, tenant or parent relation. `createdBy` is written from the
request when supplied, so historical values do not establish a verified owner.

A gateway resource check (`devbuild:{id}#can_rerun`) would therefore have to be
fed by something we do not have. Options considered and rejected: deriving
ownership from `createdBy`, inferring a project from request paths, and requiring
TiBuild to expose a new ownership view before enforcement could start.

## Decision

**The gateway authorizes API capabilities only. TiBuild keeps its own
object-level authorization.**

Registry rules for TiBuild carry a capability check and no resource check:

| Operation | Gateway check | Service check |
| --- | --- | --- |
| rerun | `capability:tibuild.devbuild.rerun#invoke` | owner / mutation policy in TiBuild |
| create | `capability:tibuild.devbuild.create#create` | TiBuild's existing checks |
| get | `capability:tibuild.devbuild.read#read` | read visibility, ADR 0002 |

This is the layered model already recorded in `ee-ops`
`docs/gcp-gateway-auth.md`: the Gateway enforces route-level principals, the
service enforces object-level ownership, tenant isolation and business roles the
Gateway cannot see. The ADR exists because the pilot briefly tried to move that
layer upward.

## Consequences

1. **The gateway answers a narrower question.** "May this caller use this API
   capability", not "may this caller act on this build". Nobody should read a
   gateway allow as ownership.
2. **TiBuild's checks are load-bearing, not defence in depth.** Removing or
   weakening them is a regression the gateway cannot compensate for. The gateway
   only sees whichever callers hold the capability.
3. **Capability grants and the route allowlist both matter.** Every caller on the
   machine route's allowlist gets the capability, so separation between callers
   still comes from that allowlist. Tightening means editing both.
4. **In-cluster calls are unaffected.** A direct call to the TiBuild Service skips
   the Gateway entirely, which is why the service's own checks must stay.
5. **Reversible if the ground truth appears.** If TiBuild later exposes a
   trustworthy build-to-project relation, a resource rule can be added to those
   routes without changing the model. Until then, adding one would deny by
   default — an unknown owner is indistinguishable from a denied one.

## Evidence

- `authzd/policies/registry-v1.json`: three TiBuild rules, capability check only.
- `authzd/internal/registry/sample_test.go`: asserts the sample has no resource
  checks, so the decision cannot regress silently.
- Decision logs before this change reported `resource_not_found` for rerun: that
  was the ADR's gap showing up at runtime, not a policy defect.
