# ADR 0001: Canonical authorization subjects

- Status: Proposed; pending Security and identity owner approval
- Date: 2026-11-24

## Context

OpenFGA subjects must be stable across email changes and must keep human and
workload trust domains distinct. Lark and GitHub identities are external
identifiers and should not become implicit authorization principals.

## Recommendation

- Human subject: `user:google/sub/<immutable-subject>`, where `<immutable-subject>`
  is the Google ID token `sub` claim. The issuer is recorded as the `google`
  token in the subject rather than as a URL; see the constraint below.
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

## OpenFGA encoding constraint (verified 2026-11-26)

OpenFGA rejects a subject whose identifier contains `:` after the type prefix.
`user:https://accounts.google.com|<sub>` returns
`validation_error: invalid relation: the 'user' field is malformed`, while
`user:google|1024…`, `user:google/sub/1024…` and the existing
`workload:google/sub/1066…` are all accepted.

Measured against the deployed store with the pinned model
(`capability:authz.canary#read`), same relation, four spellings:

| Subject | Result |
| --- | --- |
| `user:https://accounts.google.com|<sub>` | rejected, malformed |
| `user:google|<sub>` | accepted |
| `user:google/sub/<sub>` | accepted |
| `workload:google/sub/<sub>` | accepted |

Consequences:

- the human subject uses `user:google/sub/<sub>`, matching the machine form;
- an issuer URL cannot appear in a subject identifier, so issuer separation must
  be carried by a short token (`google`), not by the URL;
- changing the encoding after tuples exist means re-writing every tuple, so this
  is fixed before the first production grant;
- the same rule applies to any future issuer: use a short, stable token and keep
  the full issuer in the identity configuration, not in the subject.

## Approval

Pending: security owner and identity/service owners.
