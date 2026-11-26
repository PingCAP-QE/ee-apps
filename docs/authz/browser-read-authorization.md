# Browser read authorization for TiBuild

Design for the first browser path through external authorization. It answers three
questions the machine pilot did not: which capability covers a browser read, which
tuples that capability needs, and what the data layer keeps doing.

Status: proposal. Nothing here is enabled. The browser route carries no `extAuth`
today, and two gates below are still open.

## What the browser route can reach

The browser HTTPRoute (`v2-tibuild-v2`) matches `GET`, `HEAD` and `OPTIONS` only.
Every mutating method under `/tibuild` is served by the machine-only routes, so:

- **reads are the whole browser surface**;
- `POST /tibuild/api/v2/devbuilds` (create) is not a browser operation, even
  though the Phase 7 plan lists it next to get/list. Its rule belongs on the
  machine route.

External paths that matter for reads, after the `/tibuild` → `/` rewrite:

| Operation | External path | Read scope |
| --- | --- | --- |
| List builds | `GET /tibuild/api/v2/devbuilds` | multi-row, query filters |
| Read one build | `GET /tibuild/api/v2/devbuilds/{id}` | single row |
| Capabilities | `GET /tibuild/api/v2/devbuilds/capabilities` | what the caller may do |

> **Status note (2026-11-26).** The owner decided that browser reads stay open to
> any authenticated employee (ADR 0002) and that groups will not be queried at all;
> future special-operation APIs are authorized per individual. This contract is
> therefore not on the roadmap. It is kept because it records the reasoning for the
> boundary below, which still applies to any future browser authorization.

## Scope boundary: authorization only

This contract decides *who may act*, never *who someone is*. Authentication stays
where it is: the Gateway verifies OIDC sessions and Google ID tokens, `authzd`
independently verifies the token it is given, and nothing here changes issuer,
audience, session handling or claims parsing.

That boundary rules out an otherwise tempting shortcut. A Google ID token carries
`hd: <workspace-domain>`, so "any account in the domain" could be treated as "any
employee" with no directory access at all. It is rejected:

| Approach | Why it is out of scope |
| --- | --- |
| Grant access when a verified claim such as `hd` matches a domain | Uses an identity attribute as the authority for access. It belongs to authentication, it cannot express a team, and revocation, per-member audit and future granularity all disappear |
| Revoke access by requiring an email suffix | Same problem, plus the value is display metadata |
| Read a Workspace group and emit `team#member` tuples | **In scope**: the identity system is the data source, the authorization model still decides, and every grant is a reviewable, revocable tuple |

So the only accepted source for "all staff" is a membership relation with an
authoritative owner, synced into tuples — see
`docs/authz/workspace-group-membership.md`. If a future change needs a
claim-based check, it is an authentication change and belongs in a different
track, not here.

## Capability, not resource

ADR 0002 keeps read visibility as it is: an authenticated employee can read any
build. Object-level read filtering (`devbuild#can_read`) is therefore **not** part
of this contract — it would change visibility, and `devbuild` parent/creator
tuples have no trustworthy source (ADR 0005).

So a browser read is one **capability** check, no resource check:

| Operation | Check | Relation |
| --- | --- | --- |
| list / get / capabilities | `capability:tibuild.devbuild.read` | `read` |

`read` resolves to `caller` in the model, so the only tuple the check needs is
`capability:tibuild.devbuild.read#caller`.

Deliberately one capability for all three operations: they expose the same data
under the same visibility rule. Splitting them would suggest a distinction the
product does not make. When a distinction appears (for example a separate
`scope=mine` mode), it gets its own capability then, not now.

## Tuples required

| Tuple | Owner | Status |
| --- | --- | --- |
| `capability:tibuild.devbuild.read#caller@team:<staff-group>#member` | reviewed grants file | writable today |
| `team:<staff-group>#member@user:google/sub/<sub>` per employee | directory sync | **not taken** — the owner decided against group lookup; see the note above |

The second row is the blocker. "Every authenticated employee" is a set, and the
only honest way to express it is a team whose membership comes from an
authoritative source. Candidates, with the trade-off:

| Source | Fit | Trade-off |
| --- | --- | --- |
| Google Workspace group (e.g. an all-staff group) | Directory API lists members; stable Google subjects already exist in that directory | Coarse (org-wide), needs a read-only contract and a sync job; a group that includes external contractors silently widens access |
| `roster` database (Lark groups) | Already synced, has `group_path` | Organizational reporting data; no Google `sub` mapping |
| Reviewed static list | Exact, reviewable in Git | Does not scale, goes stale |

Recommendation: decide the source explicitly (Workspace group preferred), then
implement one poll-based sync with the completeness rules already implemented in
`internal/relationships` — a snapshot that is not complete never prunes, and an
empty read is refused unless an operator opts in.

Until that exists, the capability tuple can be granted only to specific people or
to a workload, which is not the intended "all staff" semantic. That is why
browser `extAuth` stays off: enabling it now would deny every browser read.

## Registry additions

Each read operation gets a rule bound to the browser route, in `observe` until the
divergence review passes:

```json
{"id":"tibuild-devbuild-list","host":"prow.tidb.net","route":"v2-tibuild-v2",
 "method":"GET","path":"/tibuild/api/v2/devbuilds","priority":100,"mode":"observe",
 "checks":[{"object":"capability:tibuild.devbuild.read","relation":"read"}]}
```

`route` must be the browser HTTPRoute name (`v2-tibuild-v2`), not the machine one;
the registry matches route identity, and a rule with the wrong route never matches.

Deliberately absent: a rule for `GET /tibuild/api/v2/devbuilds/capabilities` that
returns what the caller may do. Deciding it needs the same capability as the reads
it describes is reasonable, but it is a product decision — record it before the
rule is added, or the endpoint answers 403 while the reads it advertises succeed.

## What the data layer keeps

Unchanged, and not replaceable by a gateway allow:

- list endpoints keep their row filters and `scope` handling;
- the service keeps owner and mutation checks for machine callers;
- the gateway decides "may this principal call this read", not "which rows".

A single allow cannot filter a heterogeneous list, and the design document says so
explicitly. Anyone who reads this contract later should not assume the gateway
protects list content.

## Gates before enabling

1. Membership source decided and synced (the tuple table above).
2. Registry rules reviewed, including the capabilities-endpoint decision.
3. `filterOrder` promoted to the shared proxy, and the browser `forwardIDToken`
   contract verified there (Phase B, mode 2).
4. Browser `extAuth` attached in `observe`, divergence classified, then `enforce`
   for reads only.
5. Negative tests: unauthenticated, expired session, wrong audience, no capability
   tuple, and a machine token replayed against the browser route.

## Open questions for the owners

1. Which group defines "all staff", and who owns its membership?
2. Does the capabilities endpoint follow the read capability, or stay open to any
   authenticated session?
3. Do browser reads need `scope=mine` to become enforcing (that would be a
   product change and a separate capability), or does uniform visibility stay?
