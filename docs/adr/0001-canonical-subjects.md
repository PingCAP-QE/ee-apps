# ADR 0001: Canonical authorization subjects

- Status: Proposed; pending Security and identity owner approval
- Date: 2026-11-24

## Context

OpenFGA subjects must be stable across email changes and must keep human and
workload trust domains distinct. Lark and GitHub identities are external
identifiers and should not become implicit authorization principals.

## Recommendation

- Human subject: `user:<issuer>|<immutable-subject>`; for Google users, issuer is
  `https://accounts.google.com` and the ID token `sub` claim is the key.
- Google service account: `workload:gcp/<project>/<service-account-email>` after
  signed issuer/audience validation.
- Kubernetes workload: `workload:<cloud>/<cluster>/<namespace>/<serviceaccount>`.
- Lark/GitHub identities map to a human subject through an explicitly managed
  link; email is display metadata only.
- Identity adapters must reject unverified identity headers and must not merge
  principals from browser and machine credential modes.

## Consequences

Existing tuples need an explicit migration/import map. Workload names include a
trust domain so same-named service accounts from different clusters do not
collide. This is a recommendation only; no identity handoff is enabled until
the security owner approves it and the gateway header/token evidence exists.

## Approval

Pending: security owner and identity/service owners.
