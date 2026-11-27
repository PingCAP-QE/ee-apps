# Registry coverage for the TiBuild machine route

Every operation that can reach the machine route needs a registry rule before
enforcement is considered: a path with no rule is denied, so an uncovered operation
would break the moment the mode flips.

The runtime operation list comes from `ee-ops`
`docs/authz/inventory/services-and-routes.md` (generated OpenAPI plus source
wiring). This file records how the rules map onto it and what the naming means.

## Coverage

| Family | Operations | Rule ids |
| --- | --- | --- |
| devbuilds | list, create, get, rerun, capabilities | `tibuild-devbuild-*` |
| artifact sync | list, get, start | `tibuild-artifact-sync-*` |
| hotfix | bump (2), read tag | `tibuild-hotfix-*` |
| publish | images, tiup (3 mutations + 1 read), units (2 reads + retry) | `tibuild-publish-*` |
| tidbcloud | component versions, kernel images, image builds | `tibuild-tidbcloud-*` |

22 runtime operations, 22 rules, no gaps. Reads that arrive on the browser route do
not need a rule — that route carries no `extAuth` — but the same read with a Bearer
token matches the machine route and does.

## Naming

The object names the privilege, the relation names the action level:

| Action level | Object suffix | Relation |
| --- | --- | --- |
| read | `.read` | `read` |
| creating something | `.create` | `create` |
| any other mutation | none | `invoke` |

Reads must be a separate object from mutations. One tuple on `caller` grants every
alias of that object (`invoke`, `read`, `create`), so a shared object would make a
read grant imply a write grant. Operations that differ only in business policy —
create versus rerun someone else's build — share an object, because the service
decides the rest.

## What is not decided here

- **Mode**: every rule is `observe`. Nothing is enforced.
- **Grants**: the rules name capabilities; whether any caller holds them is the
  grants file's business. A rule with no tuple denies under `enforce`.
- **Ownership**: no rule carries a resource check (ADR 0005).

## Before flipping to `enforce`

1. Every caller the route allowlist names holds the capabilities it needs.
2. The decision log for a representative window shows no `route_not_registered`
   for an operation that is genuinely in use.
3. `relationship_denied` entries are understood — either a caller missing a grant or
   a decision to deny.
